# Loaded by Bash inside the SSH terminal. No files are installed in the login home.
if [[ -r ~/.bashrc ]]; then source ~/.bashrc; fi
set +H
shopt -s cmdhist lithist interactive_comments
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
    # List the latest entry without changing the login shell's timestamp format.
    # Turning history off in the subshell lets fc list a first, single-line entry.
    line=$(set +o history; builtin fc -l -- -0 -0 2>/dev/null) || return 1
    [[ $line =~ ^([0-9]+)$'\t'[\ \*]?(.*)$ ]] || return 1
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
    local count=$1 chunk encoded='' ticket directory script n
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
    local count=''
    if ! IFS= read -rs -t 6 count; then count=''; fi
    if [[ $count == 0 ]]; then
        __ow_emit P "$__ow_user" "$PWD"
        return
    fi
    if ! __ow_read_step "$count"; then
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

__ow_cancel_dispatch() {
    [[ -n ${__ow_pending_ticket:-} ]] || return
    READLINE_LINE=''
    READLINE_POINT=0
    unset __ow_pending_ticket __ow_pending_directory __ow_pending_script
    __ow_emit P "$__ow_user" "$PWD"
}

ssh() {
    if [[ ! -t 0 || ! -t 1 || $BASH_SUBSHELL != 0 ]]; then
        command ssh "$@"
        return
    fi
    local destination='' port='' host account launcher parsing_options=1
    local key value identity='' resolved_host='' resolved_port=''
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
    account=''
    if [[ $destination == *@* ]]; then account=${destination%@*}; fi
    while read -r key value; do
        case $key in
            user) identity=$value ;;
            hostname) resolved_host=$value ;;
            port) resolved_port=$value ;;
        esac
    done < <(command ssh -G "${original[@]}" </dev/null 2>/dev/null)
    if [[ $resolved_port =~ ^[0-9]{1,5}$ ]]; then port=$resolved_port; fi
    if [[ -n $identity && -n $resolved_host && -n $port ]]; then
        __ow_emit N "$host" "$port" "$account" "$identity" "$resolved_host"
    fi
    launcher="OW_ROUTE='${OW_ROUTE:-}' OW_HOP_HOST='$host' OW_HOP_PORT='$port' OW_BOOT=$OW_BOOT OW_FILE_BOOT=$OW_FILE_BOOT OW_NONCE=$OW_NONCE exec /bin/bash -c 'exec /bin/bash --rcfile <(printf %s \"\$OW_BOOT\" | base64 -d) -i'"
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
        if [[ -t 0 && -t 1 && $BASH_SUBSHELL == 0 ]]; then __ow_emit U "$2"; fi
        command su - "$2" --session-command "OW_ROUTE='${OW_ROUTE:-}' OW_PARENT=$parent OW_BOOT=$OW_BOOT OW_FILE_BOOT=$OW_FILE_BOOT OW_NONCE=$OW_NONCE exec /bin/bash -c 'exec /bin/bash --rcfile <(printf %s \"\$OW_BOOT\" | base64 -d) -i'"
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

__ow_files() {
    if [[ -n ${READLINE_LINE:-} ]]; then
        __ow_emit J '终端有未完成的输入，请先执行或清空后回到提示符'
        return
    fi
    if [[ ! -x /usr/bin/python3 || -z ${OW_FILE_BOOT:-} ]]; then
        __ow_emit J '目标环境需要 /usr/bin/python3 才能进行文件操作'
    else
        /usr/bin/python3 -c 'import base64,os;exec(base64.b64decode(os.environ["OW_FILE_BOOT"]))'
    fi
    __ow_emit P "$__ow_user" "$PWD"
}

__ow_record_switch
unset OW_PARENT
bind -m emacs-standard -x '"\C-x\C-f":__ow_files'
bind -m vi-insert -x '"\C-x\C-f":__ow_files'
bind -m emacs-standard -x '"\C-x\C-g":__ow_dispatch'
bind -m vi-insert -x '"\C-x\C-g":__ow_dispatch'
bind -m emacs-standard -x '"\C-x\C-k":__ow_cancel_dispatch'
bind -m vi-insert -x '"\C-x\C-k":__ow_cancel_dispatch'
PROMPT_COMMAND=(__ow_prompt "${PROMPT_COMMAND[@]}")
PS0="${PS0:-}"'${__ow_pending_ticket:+$(__ow_emit B "$__ow_pending_ticket")}'
PS2="${PS2:-}"'${__ow_pending_ticket:+$(__ow_emit I "$__ow_pending_ticket")}'
