#!/usr/bin/env bash

set -euo pipefail

readonly API_PREFIX=/api/v1
SMOKE_TMP=
SMOKE_PROJECT_ID=

refuse() {
    printf 'refused: %s\n' "$1" >&2
    return 64
}

require_contract() {
    local name
    for name in SMOKE_BASE_URL SMOKE_WEB_URL SMOKE_BEARER_TOKEN SMOKE_RUN_ID; do
        [[ -n ${!name:-} ]] || {
            refuse "$name is required"
            return
        }
    done
    [[ $SMOKE_BASE_URL == https://* ]] || {
        refuse 'SMOKE_BASE_URL must use https://'
        return
    }
    [[ $SMOKE_WEB_URL == https://* ]] || {
        refuse 'SMOKE_WEB_URL must use https://'
        return
    }
    command -v jq >/dev/null || {
        refuse 'jq is required for response validation'
        return
    }
}

if [[ ${SMOKE_TEST_OVERRIDE:-} != 1 ]] || ! declare -F http_request >/dev/null; then
    http_request() {
        local method=$1 url=$2 output_file=$3
        shift 3
        curl --silent --show-error \
            --output "$output_file" \
            --write-out '%{http_code}' \
            --request "$method" \
            --header "Authorization: Bearer $SMOKE_BEARER_TOKEN" \
            "$@" \
            "$url"
    }
fi

if [[ ${SMOKE_TEST_OVERRIDE:-} != 1 ]] || ! declare -F public_http_request >/dev/null; then
    public_http_request() {
        local method=$1 url=$2 output_file=$3
        shift 3
        curl --silent --show-error \
            --output "$output_file" \
            --write-out '%{http_code}' \
            --request "$method" \
            "$@" \
            "$url"
    }
fi

expect_2xx() {
    local operation=$1 status=$2 response_file=$3
    if [[ $status =~ ^2[0-9][0-9]$ ]]; then
        return
    fi
    printf '%s returned HTTP %s: ' "$operation" "$status" >&2
    tr '\n' ' ' <"$response_file" >&2
    printf '\n' >&2
    return 1
}

cleanup_project() {
    local response status
    [[ -n ${SMOKE_PROJECT_ID:-} ]] || return 0
    response="$SMOKE_TMP/cleanup.json"
    status=$(http_request DELETE \
        "$SMOKE_BASE_URL$API_PREFIX/projects/$SMOKE_PROJECT_ID" "$response") || return 1
    expect_2xx cleanup "$status" "$response" || return
    printf 'cleanup=deleted project_id=%s\n' "$SMOKE_PROJECT_ID"
    SMOKE_PROJECT_ID=
}

cleanup_on_exit() {
    local original_status=$?
    if [[ -n ${SMOKE_PROJECT_ID:-} ]]; then
        cleanup_project || printf 'cleanup failed for project_id=%s\n' "$SMOKE_PROJECT_ID" >&2
    fi
    [[ -z ${SMOKE_TMP:-} ]] || rm -rf -- "$SMOKE_TMP"
    return "$original_status"
}

main() {
    local response status session_id project_name entity_iri baseline changed

    require_contract || return
    SMOKE_BASE_URL=${SMOKE_BASE_URL%/}
    SMOKE_WEB_URL=${SMOKE_WEB_URL%/}
    SMOKE_TMP=$(mktemp -d)
    trap cleanup_on_exit EXIT

    response="$SMOKE_TMP/health.json"
    status=$(http_request GET "$SMOKE_BASE_URL/health" "$response")
    expect_2xx health "$status" "$response"
    [[ $(jq -r '.status // empty' "$response") == healthy ]] || \
        refuse 'health response was not healthy'

    response="$SMOKE_TMP/projects.json"
    status=$(http_request GET "$SMOKE_BASE_URL$API_PREFIX/projects?limit=1" "$response")
    expect_2xx 'projects API' "$status" "$response"
    jq -e '.items | type == "array"' "$response" >/dev/null || \
        refuse 'projects API response did not contain an items array'

    response="$SMOKE_TMP/ui.html"
    # This is a public UI probe. Never send the API bearer token to the web
    # origin, even when both URLs currently share a hostname.
    status=$(public_http_request GET "$SMOKE_WEB_URL/projects" "$response")
    expect_2xx 'web projects route' "$status" "$response"

    project_name="ci-smoke-${SMOKE_RUN_ID//[^A-Za-z0-9_-]/-}"
    baseline="$SMOKE_TMP/baseline.ttl"
    changed="$SMOKE_TMP/changed.ttl"
    entity_iri="https://smoke.ontokit.invalid/${SMOKE_RUN_ID//[^A-Za-z0-9_-]/-}#MintedClass"
    printf '%s\n' \
        '@prefix owl: <http://www.w3.org/2002/07/owl#> .' \
        '@prefix smoke: <https://smoke.ontokit.invalid/ontology#> .' \
        'smoke:Ontology a owl:Ontology .' >"$baseline"
    cp "$baseline" "$changed"
    printf '%s\n' \
        '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .' \
        "<$entity_iri> a <http://www.w3.org/2002/07/owl#Class> ;" \
        '  rdfs:label "Release smoke minted class"@en .' >>"$changed"

    response="$SMOKE_TMP/import.json"
    status=$(http_request POST "$SMOKE_BASE_URL$API_PREFIX/projects/import" "$response" \
        --form "file=@$baseline;type=text/turtle" \
        --form 'is_public=false' \
        --form "name=$project_name" \
        --form 'description=Dedicated ephemeral release-promotion smoke project')
    expect_2xx 'throwaway project import' "$status" "$response"
    SMOKE_PROJECT_ID=$(jq -r '.id // empty' "$response")
    [[ $SMOKE_PROJECT_ID =~ ^[0-9a-fA-F-]{36}$ ]] || \
        refuse 'throwaway project import returned no UUID'

    response="$SMOKE_TMP/session.json"
    status=$(http_request POST \
        "$SMOKE_BASE_URL$API_PREFIX/projects/$SMOKE_PROJECT_ID/suggestions/sessions" \
        "$response" --header 'Content-Type: application/json' --data '{}')
    expect_2xx 'session mint' "$status" "$response"
    session_id=$(jq -r '.session_id // empty' "$response")
    [[ $session_id =~ ^[A-Za-z0-9_-]+$ ]] || refuse 'session mint returned an unsafe ID'

    response="$SMOKE_TMP/save.json"
    status=$(jq -n \
        --rawfile content "$changed" \
        --arg entity_iri "$entity_iri" \
        '{content: $content, entity_iri: $entity_iri, entity_label: "Release smoke minted class", mints_entity: true}' |
        http_request PUT \
            "$SMOKE_BASE_URL$API_PREFIX/projects/$SMOKE_PROJECT_ID/suggestions/sessions/$session_id/save" \
            "$response" --header 'Content-Type: application/json' --data-binary @-)
    expect_2xx 'mint/save' "$status" "$response"

    response="$SMOKE_TMP/submit.json"
    status=$(http_request POST \
        "$SMOKE_BASE_URL$API_PREFIX/projects/$SMOKE_PROJECT_ID/suggestions/sessions/$session_id/submit" \
        "$response" --header 'Content-Type: application/json' \
        --data '{"summary":"Automated immutable-release smoke"}')
    if [[ $status == 422 ]]; then
        printf 'submit returned HTTP 422: ' >&2
        tr '\n' ' ' <"$response" >&2
        printf '\n' >&2
        return 1
    fi
    expect_2xx submit "$status" "$response"

    cleanup_project
    printf 'smoke=passed project=%s revision_contract=immutable\n' "$project_name"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
