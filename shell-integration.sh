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
__ow_emit() {
    local payload
    payload=$(printf '%s\0' "$@" | base64 -w0) || return
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
    if __ow_latest_command && [[ $__ow_latest_number != "$__ow_history" ]]; then
        __ow_history=$__ow_latest_number
        __ow_emit H "$__ow_user" "$__ow_prior" "$status" "$__ow_latest_script"
    fi
    __ow_prior=$PWD
    __ow_emit P "$__ow_user" "$PWD"
    return "$status"
}

__ow_dispatch() {
    local count chunk encoded='' ticket directory script result n tty_mode
    [[ -z ${READLINE_LINE:-} ]] || return
    __ow_emit D
    IFS= read -rs -t 6 count || return
    [[ $count =~ ^[0-9]{1,2}$ && $count -ge 1 && $count -le 32 ]] || return
    for ((n=0; n<count; n++)); do
        IFS= read -rs -t 6 chunk || return
        encoded+=$chunk
    done
    {
        IFS= read -r -d '' ticket && IFS= read -r -d '' directory && IFS= read -r -d '' script
    } < <(printf %s "$encoded" | base64 -d) || return
    printf '\n$ %s\n' "$script"
    tty_mode=$(stty -g <&0 2>/dev/null) || tty_mode=''
    # Each saved instruction starts in its own captured directory and the verified user identity.
    (
        if [[ -n $tty_mode ]]; then
            trap 'stty "$tty_mode" <&0 2>/dev/null' EXIT
            stty icanon echo isig icrnl -igncr -inlcr <&0
        fi
        builtin cd -- "$directory" && builtin eval "$script"
    )
    result=$?
    if [[ -n $tty_mode ]]; then stty "$tty_mode" <&0 2>/dev/null; fi
    __ow_emit H "$__ow_user" "$directory" "$result" "$script"
    __ow_emit R "$ticket" "$result"
    __ow_emit P "$__ow_user" "$PWD"
    return "$result"
}

unalias su 2>/dev/null || true
su() {
    if [[ $# == 2 && ($1 == '-' || $1 == '-l' || $1 == '--login') && $2 =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*[$]?$ ]]; then
        local parent='' direct='^[[:blank:]]*su[[:blank:]]+(-|-l|--login)[[:blank:]]+([a-zA-Z_][a-zA-Z0-9_.-]*[$]?|"[a-zA-Z_][a-zA-Z0-9_.-]*[$]?"|'\''[a-zA-Z_][a-zA-Z0-9_.-]*[$]?'\'')[[:blank:]]*$' status
        if __ow_latest_command && [[ $__ow_latest_number != "$__ow_history" && $__ow_latest_script =~ $direct ]]; then
            parent=$(printf '%s\0' "$__ow_user" "$PWD" "$__ow_latest_script" "$2" | base64 -w0)
            __ow_history=$__ow_latest_number
        fi
        command su - "$2" --session-command "OW_PARENT=$parent OW_BOOT=$OW_BOOT OW_NONCE=$OW_NONCE exec /bin/bash -c 'exec /bin/bash --rcfile <(printf %s \"\$OW_BOOT\" | base64 -d) -i'"
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
