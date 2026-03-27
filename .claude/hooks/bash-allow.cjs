//to test this hook, run "echo '{"tool_name":"Bash","tool_input":{"command":"npm run build"}}' | node .claude/hooks/bash-allow.cjs"
const fs = require('fs');
const path = require('path');

const MAX_LOG_SIZE = 1 * 1024 * 1024;  // 1MB
const MAX_LOG_FILES = 5;
const LOG_DIR = path.join(__dirname, '..', 'logs');
const LOG_FILE = path.join(LOG_DIR, 'hook-debug.log');

function ensureLogDir() {
    fs.mkdirSync(LOG_DIR, { recursive: true });
}

function rotate() {
    const oldest = `${LOG_FILE}.${MAX_LOG_FILES}`;
    try { fs.unlinkSync(oldest); } catch {}
    for (let i = MAX_LOG_FILES - 1; i >= 1; i--) {
        try { fs.renameSync(`${LOG_FILE}.${i}`, `${LOG_FILE}.${i + 1}`); } catch {}
    }
    try { fs.renameSync(LOG_FILE, `${LOG_FILE}.1`); } catch {}
}

function log(message) {
    const line = `${new Date().toISOString()} ${message}\n`;
    console.error(line.trimEnd());
    try {
        const stats = fs.statSync(LOG_FILE);
        if (stats.size >= MAX_LOG_SIZE) rotate();
    } catch {}
    fs.appendFileSync(LOG_FILE, line);
}

ensureLogDir();

const chunks = [];
process.stdin.on('data', d => chunks.push(d));
process.stdin.on('end', () => {
    try {
        let input = Buffer.concat(chunks).toString('utf8');
        input = input.replace(/^\uFEFF/, '');
        const { tool_input } = JSON.parse(input);
        const cmd = (tool_input.command || "").toLowerCase();

        log(`CMD: ${cmd}`);

    const ALLOWED_PREFIXES = [
        "npm ", "npx ", "git ", "cat ", "echo ", "find ", "head ", "tail ",
        // File inspection (read-only)
        "ls ", "dir ", "type ",        // list/read files
        "pwd",                          // print working dir
        "node ", "node.",               // run node scripts
        "tsc ",                         // typescript compiler
        // Text processing (read-only)
        "grep ", "rg ",                 // search
        "sort ", "uniq ", "wc ",        // text utils
        "more ", "less ",               // pagers
        "time",                         // timing processes
        // Environment inspection
        "where ", "which ",             // find executables
        "node --version", "npm --version",
    ];

    const BLOCKED_STRINGS = [
        "rm -rf", "del /f", "del /q", "rd /s", "rmdir /s", "format ",
        "sudo ", "runas ", "curl ", "wget ", "invoke-webrequest",
        "invoke-restmethod", "powershell -e", "powershell -enc",
        "eval ", "setx ", "reg add", "reg delete",
        "git push --force", "git reset --hard", "git clean -f",
    ];

    const BLOCKED_PATTERNS = [
        /\|\s*(bash|sh|cmd|powershell)/,  // pipe to shell
        />\s*(\/etc|\/bin|C:\\Windows)/i, // redirect to system dirs
    ];

    const blocked = BLOCKED_STRINGS.some(p => cmd.includes(p)) || BLOCKED_PATTERNS.some(r => r.test(cmd));
    const allowed = ALLOWED_PREFIXES.some(p =>
        cmd.split(/&&|\|/).map(s => s.trim()).some(segment => segment.startsWith(p))
    );

    const decision = blocked ? 'deny' : allowed ? 'allow' : 'ask';
    log(`DECISION: ${decision}`);

    // Output JSON to stdout so Claude Code acts on the decision
    if (decision !== 'ask') {
        const reason = blocked
            ? `Blocked: "${cmd}" contains dangerous patterns`
            : `Allowed: matches safe command prefix`;
        process.stdout.write(JSON.stringify({
            hookSpecificOutput: {
                hookEventName: 'PreToolUse',
                permissionDecision: decision,
                permissionDecisionReason: reason,
            }
        }));
    }

    } catch(err) {
        log(`ERROR: ${err.message}`);
    }
});
