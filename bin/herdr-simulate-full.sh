#!/usr/bin/env bash
#
# Simulação completa do ciclo herdr-review -> correção -> herdr-review ->
# terceira análise automática com herdr-ask --reviewer scout. O fixture é
# sintético e é enviado por --files; nenhum arquivo do projeto é alterado.
#
# Rode dentro do pane <slug>-exec. O scout é acionado automaticamente depois
# das duas rodadas, reproduzindo a política do fluxo real.
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Uso: herdr-simulate-full.sh <slug> [opções]

Executa, em um space de teste, o ciclo completo:
  1. revisão cega com rev-1 e rev-2;
  2. correção sintética do fixture;
  3. segunda revisão cega com rev-1 e rev-2;
  4. terceira análise automática pelo scout;
  5. registro dos artefatos e da decisão pendente do Breno.

O slug deve ter <slug>-exec, <slug>-rev-1, <slug>-rev-2 e <slug>-scout já
existentes. A execução deve partir do pane <slug>-exec, como os dispatchers
normais. O fixture fica em /tmp e é passado por --files.

Opções:
  --timeout SEGUNDOS       timeout de cada dispatcher (default: 300)
  --skip-tests             não roda a suíte antes do ciclo
  --offline                prepara fixture e comandos, mas não toca nos agents
  --allow-production       permite slugs conhecidos de produção (evite usar)
  -h, --help               mostra esta ajuda

Exemplos:
  herdr-simulate-full.sh herdr-pilot --offline
  herdr-simulate-full.sh herdr-pilot
EOF
}

die() {
    echo "herdr-simulate-full: $*" >&2
    exit 2
}

if (($# == 0)); then
    usage >&2
    exit 2
fi
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi

slug=$1
shift
timeout_s=300
skip_tests=0
offline=0
allow_production=0

while (($#)); do
    case "$1" in
        --timeout)
            (($# >= 2)) || die "--timeout exige um número"
            timeout_s=$2
            shift 2
            ;;
        --skip-tests)
            skip_tests=1
            shift
            ;;
        --offline)
            offline=1
            shift
            ;;
        --allow-production)
            allow_production=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "opção desconhecida: $1 (use --help)"
            ;;
    esac
done

[[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "slug inválido: use letras, números, ponto, sublinhado ou hífen"
[[ "$timeout_s" =~ ^[0-9]+$ ]] && ((timeout_s > 0)) \
    || die "--timeout deve ser um inteiro maior que zero"

case "$slug" in
    mfc|homehub|herdr|omabackup|claude-bridge|llm|llm-bench|omasession|omaspotlight|dre)
        ((allow_production)) || die "slug '$slug' parece ser de produção; use um space descartável ou --allow-production"
        ;;
esac

caller_cwd=$(pwd -P)
script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
tool_root=$(cd "$(dirname "$script_path")/.." && pwd -P)
review_script="$tool_root/bin/herdr-review-dispatch"
ask_script="$tool_root/bin/herdr-ask"
[[ -f "$review_script" ]] || die "não encontrei $review_script"
[[ -f "$ask_script" ]] || die "não encontrei $ask_script"

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/herdr-ciclo-simulacao.XXXXXX")
fixture="$run_dir/settle.py"
description1="$run_dir/descricao-rodada-1.md"
description2="$run_dir/descricao-rodada-2.md"
scout_question="$run_dir/duvida-para-scout.md"
decision_file="$run_dir/decision.md"

cat >"$fixture" <<'EOF'
"""Fixture sintético: a primeira versão deixa uma disputa de intent aberta."""


def settle_retroactive(intents):
    """Escolhe o último intent UNCERTAIN para o mesmo deal retroativo."""
    winner = None
    for intent in intents:
        if intent.get("deal_id") == "retro" and intent.get("status") == "UNCERTAIN":
            winner = intent
    return winner
EOF

cat >"$description1" <<'EOF'
Simulação deliberada do ciclo completo. Este fixture contém uma disputa:
dois intents UNCERTAIN podem apontar para o mesmo deal retroativo, e a função
escolhe silenciosamente o último. Identifique os riscos reais e registre-os
no veredito sem editar o fixture.
EOF

cat >"$description2" <<'EOF'
Simulação da segunda rodada de correção. O exec aplicou uma correção parcial
no fixture depois da primeira rodada, mas a decisão sobre a disputa ainda pode
estar aberta. Verifique somente o estado atual e registre qualquer achado que
permaneça ou tenha surgido nas linhas alteradas.
EOF

echo "space alvo: $slug"
echo "cwd da consulta: $caller_cwd"
echo "fixture: $fixture"
echo "artefatos da simulação: $run_dir"

if (( ! skip_tests )); then
    echo
    echo "== suíte do herdr-mesh-tools =="
    (cd "$tool_root" && python3 -m unittest discover -s tests -q)
fi

review_dir=""
run_review_round() {
    local label=$1
    local description_file=$2
    local log="$run_dir/${label}.log"
    local rc

    echo
    echo "== $label: rev-1 + rev-2 =="
    set +e
    (
        cd "$caller_cwd"
        python3 "$review_script" "$slug" \
            --description-file "$description_file" \
            --files "$fixture" \
            --timeout "$timeout_s"
    ) 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e

    review_dir=$(sed -nE 's/^round dir: (.*)$/\1/p' "$log" | tail -n 1 || true)
    if ((rc != 0)); then
        echo "$label falhou com código $rc; log: $log" >&2
        return "$rc"
    fi
    [[ -n "$review_dir" && -d "$review_dir" ]] \
        || { echo "$label não produziu round dir; log: $log" >&2; return 1; }
    echo "$label concluída: $review_dir"
}

if ((offline)); then
    cat >"$scout_question" <<EOF
# Dúvida da simulação para o scout

As duas rodadas de rev-1/rev-2 serão executadas antes desta pergunta. Use o
fixture abaixo e declare a incerteza se a evidência não bastar.

## Fixture

$(cat "$fixture")
EOF
    echo
    echo "== modo offline =="
    echo "não serão resetados nem acionados agents"
    echo
    echo "comandos que seriam executados:"
    printf '  %q' python3 "$review_script" "$slug" --description-file "$description1" --files "$fixture" --timeout "$timeout_s"
    echo
    printf '  %q' python3 "$review_script" "$slug" --description-file "$description2" --files "$fixture" --timeout "$timeout_s"
    echo
    printf '  %q' python3 "$ask_script" "$slug" --question-file "$scout_question" --reviewer scout --context "$fixture"
    echo
    echo "fixture e descrições: $run_dir"
    exit 0
fi

command -v herdr >/dev/null 2>&1 || die "comando herdr não está no PATH"

echo
echo "== pré-checagem dos papéis =="
for role in rev-1 rev-2 scout; do
    if ! herdr agent get "${slug}-${role}" >/dev/null 2>&1; then
        die "agent '${slug}-${role}' não foi encontrado"
    fi
    echo "${slug}-${role} encontrado"
done

run_review_round "rodada-1" "$description1" || exit $?
r1_dir="$review_dir"

echo
echo "== correção sintética entre as rodadas =="
cat >"$fixture" <<'EOF'
"""Fixture sintético após uma correção parcial."""


def settle_retroactive(intents):
    """Evita escolher silenciosamente quando há mais de um UNCERTAIN."""
    candidates = [
        intent for intent in intents
        if intent.get("deal_id") == "retro" and intent.get("status") == "UNCERTAIN"
    ]
    if len(candidates) != 1:
        return {"status": "UNCERTAIN", "candidates": candidates}
    return candidates[0]
EOF
echo "fixture atualizado; o segundo dispatch verá esta versão"

run_review_round "rodada-2" "$description2" || exit $?
r2_dir="$review_dir"

mapfile -t verdicts < <(
    find "$r1_dir" "$r2_dir" -mindepth 2 -maxdepth 2 -type f -name verdict.md | sort
)
(( ${#verdicts[@]} == 4 )) \
    || die "esperava quatro vereditos (2 rodadas x 2 revisores), encontrei ${#verdicts[@]}"

{
    cat <<EOF
# Dúvida da simulação para o scout

As duas rodadas de rev-1/rev-2 terminaram. Esta é a terceira análise automática
do ciclo. Analise a disputa abaixo usando apenas o
fixture e os quatro vereditos congelados. Devolva a resposta ao ${slug}-exec;
se a evidência não bastar, registre a incerteza para ele levar ao Breno.

## Fixture atual

EOF
    cat "$fixture"
    for verdict in "${verdicts[@]}"; do
        echo
        echo "## Veredito: $verdict"
        cat "$verdict"
    done
} >"$scout_question"

cat >"$decision_file" <<EOF
# Registro de decisão da simulação

- rodada 1: $r1_dir
- rodada 2: $r2_dir
- fixture: $fixture
- scout: acionado automaticamente pelo -exec
- resposta do scout: pendente
- decisão final do Breno: PENDENTE — preencher antes de qualquer commit.
EOF

echo
echo "== dúvida preparada =="
echo "pergunta: $scout_question"
echo "registro: $decision_file"

scout_log="$run_dir/scout.log"
echo
echo "== terceira análise automática: scout =="
set +e
(
    cd "$caller_cwd"
    python3 "$ask_script" "$slug" \
        --question-file "$scout_question" \
        --reviewer scout \
        --context "$fixture" "${verdicts[@]}" \
        --timeout "$timeout_s"
) 2>&1 | tee "$scout_log"
scout_rc=${PIPESTATUS[0]}
set -e

scout_dir=$(sed -nE 's/^consulta [0-9]+: (.*)$/\1/p' "$scout_log" | tail -n 1 || true)
if [[ -n "$scout_dir" && -d "$scout_dir" ]]; then
    scout_answer="$scout_dir/${slug}-scout/answer.md"
    sed -i "s#^- resposta do scout: .*#- resposta do scout: ${scout_answer}#" "$decision_file"
    echo
    echo "== resposta devolvida ao -exec =="
    echo "artefato: $scout_answer"
    if [[ -f "$scout_answer" ]]; then
        sed -n '1,260p' "$scout_answer"
    else
        echo "answer.md ainda não existe; consulte o log: $scout_log"
    fi
else
    echo "não encontrei a rodada do scout; log: $scout_log" >&2
fi

echo
echo "decisão final continua pendente em: $decision_file"
exit "$scout_rc"
