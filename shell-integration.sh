# Loaded by Bash inside the SSH terminal. No files are installed in the login home.
if [[ -r ~/.bashrc ]]; then source ~/.bashrc; fi
set +H
shopt -s cmdhist lithist
HISTFILE=/dev/null
HISTCONTROL=
HISTIGNORE=
HISTSIZE=300

__ow_user=$(id -un)
__ow_prior=$PWD
__ow_history=0
if [[ -n ${OW_HOP_HOST:-} ]]; then
    # If the command did not specify a port, sshd reports the port selected by OpenSSH.
    read -r __ow_client __ow_client_port __ow_server __ow_server_port <<< "${SSH_CONNECTION:-}"
    __ow_hop=$(printf '%s\0' "$OW_HOP_HOST" "${OW_HOP_PORT:-${__ow_server_port:-22}}" "$__ow_user" | base64 -w0)
    OW_ROUTE="${OW_ROUTE:+$OW_ROUTE.}$__ow_hop"
    unset OW_HOP_HOST OW_HOP_PORT __ow_hop __ow_client __ow_client_port __ow_server __ow_server_port
fi

__ow_emit() {
    local payload
    payload=$(printf '%s\0' "${OW_ROUTE:-}" "$@" | base64 -w0) || return
    printf '\033]777;OW:%s:%s\a' "$OW_NONCE" "$payload"
}

__ow_latest_command() {
    local line
    line=$(HISTTIMEFORMAT= builtin history 1)
    [[ $line =~ ^[[:space:]]*([0-9]+)[\ \*]+(.*)$ ]] || return 1
    __ow_latest_number=${BASH_REMATCH[1]}
    __ow_latest_script=${BASH_REMATCH[2]}
}

__ow_prompt() {
    local status=$?
    if [[ -n ${__ow_pending_ticket:-} ]]; then
        __ow_emit H "$__ow_user" "$__ow_pending_directory" "$status" "$__ow_pending_script"
        __ow_emit R "$__ow_pending_ticket" "$status"
        if __ow_latest_command && [[ $__ow_latest_script == 'builtin eval "$__ow_pending_script"' ]]; then
            builtin history -d "$__ow_latest_number"
            builtin history -s "$__ow_pending_script"
        fi
        unset __ow_pending_ticket __ow_pending_directory __ow_pending_script
        if __ow_latest_command; then __ow_history=$__ow_latest_number; fi
    elif __ow_latest_command && [[ $__ow_latest_number != "$__ow_history" ]]; then
        __ow_history=$__ow_latest_number
        __ow_emit H "$__ow_user" "$__ow_prior" "$status" "$__ow_latest_script"
    fi
    __ow_prior=$PWD
    __ow_emit P "$__ow_user" "$PWD"
    return "$status"
}

__ow_read_step() {
    local count chunk encoded='' ticket directory script n
    IFS= read -rs -t 6 count || return
    [[ $count =~ ^[0-9]{1,2}$ && $count -ge 1 && $count -le 32 ]] || return
    for ((n=0; n<count; n++)); do
        IFS= read -rs -t 6 chunk || return
        encoded+=$chunk
    done
    {
        IFS= read -r -d '' ticket && IFS= read -r -d '' directory && IFS= read -r -d '' script
    } < <(printf %s "$encoded" | base64 -d) || return
    __ow_pending_ticket=$ticket
    __ow_pending_directory=$directory
    __ow_pending_script=$script
}

__ow_dispatch() {
    if [[ -n ${READLINE_LINE:-} ]]; then
        __ow_emit X '终端还有未提交的输入。请处理后重试。'
        return
    fi
    __ow_emit D
    if ! __ow_read_step; then
        __ow_emit X '未能接收快捷指令，请重试。'
        __ow_emit P "$__ow_user" "$PWD"
        return
    fi
    if [[ $PWD != "$__ow_pending_directory" ]] && ! builtin cd -- "$__ow_pending_directory"; then
        __ow_emit H "$__ow_user" "$__ow_pending_directory" 1 "$__ow_pending_script"
        __ow_emit R "$__ow_pending_ticket" 1
        unset __ow_pending_ticket __ow_pending_directory __ow_pending_script
        __ow_emit P "$__ow_user" "$PWD"
        return
    fi
    # Submit through the normal Bash prompt, so cwd, exports, and shell definitions persist.
    if [[ $__ow_pending_script == *$'\n'* ]]; then
        # Keep a multiline script in one Bash command, with one result for the whole step.
        printf '\n$ %s\n' "$__ow_pending_script"
        READLINE_LINE='builtin eval "$__ow_pending_script"'
    else
        READLINE_LINE=$__ow_pending_script
    fi
    READLINE_POINT=${#READLINE_LINE}
    __ow_emit A "$__ow_pending_ticket"
}

ssh() {
    local destination='' port='' host launcher parsing_options=1
    local -a original=("$@")
    while (( $# )); do
        case $1 in
            --)
                if [[ -z $destination && $parsing_options == 1 ]]; then
                    parsing_options=0
                    shift
                    continue
                fi
                ;;
            -p|-p[0-9]*)
                if [[ $parsing_options == 1 && -z $port ]]; then
                    port=${1#-p}
                    if [[ $1 == -p ]]; then
                        shift
                        port=${1:-}
                    fi
                    if [[ $port =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )); then
                        shift
                        continue
                    fi
                fi
                ;;
            [a-zA-Z0-9_:\[]*)
                if [[ -z $destination ]]; then
                    destination=$1
                    shift
                    continue
                fi
                ;;
        esac
        command ssh "${original[@]}"
        return
    done
    host=${destination##*@}
    if [[ $host == \[*\] ]]; then host=${host:1:${#host}-2}; fi
    if ! [[ $host =~ ^[a-zA-Z0-9:][a-zA-Z0-9.:-]{0,251}$ &&
            ($destination == "$host" || $destination == "[$host]" || ${destination%@*} =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*[$]?$) ]]; then
        command ssh "${original[@]}"
        return
    fi
    launcher="OW_ROUTE='${OW_ROUTE:-}' OW_HOP_HOST='$host' OW_HOP_PORT='$port' OW_BOOT=$OW_BOOT OW_NONCE=$OW_NONCE exec /bin/bash -c 'exec /bin/bash --rcfile <(printf %s \"\$OW_BOOT\" | base64 -d) -i'"
    command ssh -tt "${original[@]}" "$launcher"
}

unalias su 2>/dev/null || true
su() {
    if [[ $# == 2 && ($1 == '-' || $1 == '-l' || $1 == '--login') && $2 =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*[$]?$ ]]; then
        local parent='' direct='^[[:blank:]]*su[[:blank:]]+(-|-l|--login)[[:blank:]]+([a-zA-Z_][a-zA-Z0-9_.-]*[$]?|"[a-zA-Z_][a-zA-Z0-9_.-]*[$]?"|'\''[a-zA-Z_][a-zA-Z0-9_.-]*[$]?'\'')[[:blank:]]*$' status
        if __ow_latest_command && [[ $__ow_latest_number != "$__ow_history" && $__ow_latest_script =~ $direct ]]; then
            parent=$(printf '%s\0' "$__ow_user" "$PWD" "$__ow_latest_script" "$2" | base64 -w0)
            __ow_history=$__ow_latest_number
        fi
        command su - "$2" --session-command "OW_ROUTE='${OW_ROUTE:-}' OW_PARENT=$parent OW_BOOT=$OW_BOOT OW_NONCE=$OW_NONCE exec /bin/bash -c 'exec /bin/bash --rcfile <(printf %s \"\$OW_BOOT\" | base64 -d) -i'"
        status=$?
        if [[ $status -ne 0 && -n $parent ]]; then
            # A failed switch returns to the original shell, which can record its failure normally.
            __ow_history=0
        fi
        return "$status"
    else
        command su "$@"
    fi
}

__ow_record_switch() {
    local user directory script target
    [[ -n ${OW_PARENT:-} ]] || return 0
    {
        IFS= read -r -d '' user && IFS= read -r -d '' directory &&
        IFS= read -r -d '' script && IFS= read -r -d '' target
    } < <(printf %s "$OW_PARENT" | base64 -d) || return
    if [[ $__ow_user == "$target" ]]; then
        __ow_emit S "$user" "$directory" "$script" "$target"
    fi
}

__ow_record_switch
unset OW_PARENT
bind -m emacs-standard -x '"\C-x\C-g":__ow_dispatch'
bind -m vi-insert -x '"\C-x\C-g":__ow_dispatch'
PROMPT_COMMAND=(__ow_prompt "${PROMPT_COMMAND[@]}")
