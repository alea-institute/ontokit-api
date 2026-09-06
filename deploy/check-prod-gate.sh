#!/usr/bin/env bash

set -euo pipefail

refuse() {
    printf 'refused: %s\n' "$1" >&2
    return 64
}

main() {
    if [[ ${PROD_ENABLED:-} != true ]]; then
        printf 'disabled: PROD_ENABLED is not true; promotion remains dormant\n' >&2
        return 78
    fi

    [[ -n ${PROD_DEPLOY_HOST:-} ]] || {
        refuse 'PROD_DEPLOY_HOST is required'
        return
    }
    [[ $PROD_DEPLOY_HOST =~ ^[A-Za-z0-9.-]+$ ]] || {
        refuse 'PROD_DEPLOY_HOST contains unsafe characters'
        return
    }
    [[ -n ${PROD_DEPLOY_USER:-} ]] || {
        refuse 'PROD_DEPLOY_USER is required'
        return
    }
    [[ $PROD_DEPLOY_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || {
        refuse 'PROD_DEPLOY_USER is not a safe account name'
        return
    }

    printf 'enabled: PROD gate and non-secret host contract are present\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
