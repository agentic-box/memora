"""The D1 write guard's hardened forms (docs/local-primary-implementation.md
§9 (f), slice L7): dynamically built write SQL, the remote flag on a
continuation line or in a variable, and the T5 comment/echo bypass.

Every shell or SQL fragment is assembled from pieces, because the guard
scans this repository (tests included) and must not find these examples.
"""
from __future__ import annotations

import pytest

from tests.test_l1b_inert_d1_writers import D1, REMOTE, WR, _guard, _write

UPD, DEL, INS, REP = "UP" + "DATE", "DE" + "LETE", "IN" + "SERT", "RE" + "PLACE"
DEPLOY = f"{WR} pages" + " deploy public"
GUARD_RUN = "python3 scripts/d1_write_" + "guard.py --scope all"
HANDLER = "memora-graph/functions/api/x.ts"


def _findings(tmp_path, rel, text, scope):
    _write(tmp_path, rel, text)
    r = _guard(tmp_path, scope)
    return r.returncode, r.stdout


# ---------------------------------------------------------------- handlers

@pytest.mark.parametrize("name,code,rule", [
    ("template table", f"db.prepare(`{UPD} ${{table}} SET tags = ? WHERE id = ?`).run();", "H1"),
    ("template everything", f"db.prepare(`{UPD} ${{t}} SET ${{col}} = ?`).run();", "H1"),
    ("concat update", f'db.prepare("{UPD} " + table + " SET tags = ?").run();', "H2"),
    ("concat delete", f"db.prepare('{DEL} ' + 'FROM ' + table).run();", "H2"),
    ("concat insert", f'const sql = "{INS} " + into + table; await db.prepare(sql).run();', "H2"),
    ("verb alone", f'const verb = "{REP}"; db.prepare(verb + " INTO t VALUES (?)").run();', "H2"),
    ("after interpolation", f"db.prepare(`${{prefix}}{DEL} FROM t`).run();", "H2"),
])
def test_dynamically_built_write_sql_is_found(tmp_path, name, code, rule):
    rc, out = _findings(tmp_path, HANDLER, f"export const f = async (env) => {{ const db = env.DB_MEMORA; {code} }};\n",
                        "handlers")
    assert rc == 1 and f" {rule} " in out, (name, out)
    assert "bindings: DB_MEMORA" in out


@pytest.mark.parametrize("code", [
    'const d = "Update an existing memory by ID.";',
    'const q = db.prepare("SELECT id, updated_at FROM memories WHERE id = ?");',
    "const q = db.prepare(`SELECT * FROM ${table} WHERE id = ?`);",
    'const label = "Deleted memories are shown greyed out";',
    'const status = "INSERTED_AT";',
])
def test_reads_and_prose_are_clean(tmp_path, code):
    rc, out = _findings(tmp_path, HANDLER, code + "\n", "handlers")
    assert rc == 0, out


# ---------------------------------------------------------------- tools

@pytest.mark.parametrize("name,text,rule", [
    ("remote on a continuation line", f"npx {WR}{D1}execute db \\\n  {REMOTE} --file=x.sql\n", "T2"),
    ("remote two lines down", f"npx {WR}{D1}execute db \\\n  --file=x.sql \\\n  {REMOTE}\n", "T2"),
    ("migrations apply continued", f"npx {WR}{D1}migrations apply db \\\n  {REMOTE}\n", "T4"),
    ("flags in a variable", f"FLAGS={REMOTE}\nnpx {WR}{D1}execute db $FLAGS --file=x.sql\n", "T6"),
    ("flags in a braced variable", f"npx {WR}{D1}execute db ${{MODE}} --file=x.sql\n", "T6"),
    ("migrations from a variable", f"npx {WR}{D1}migrations apply db \"$WHERE\"\n", "T6"),
])
def test_remote_flag_forms_are_found(tmp_path, name, text, rule):
    rc, out = _findings(tmp_path, "tools/x.sh", text, "tools")
    assert rc == 1 and f" {rule} " in out, (name, out)
    if "\\\n" in text:
        assert "tools/x.sh:1:" in out, "a continued command is reported at its first line"


@pytest.mark.parametrize("text", [
    f"npx {WR}{D1}execute db \\\n  --local --file=x.sql\n",
    f"npx {WR}{D1}migrations apply db \\\n  --local\n",
    f"npx {WR}{D1}execute db --local --file=$SQL_FILE\n",
])
def test_local_forms_are_clean(tmp_path, text):
    rc, out = _findings(tmp_path, "tools/ok.sh", text, "tools")
    assert rc == 0, out


@pytest.mark.parametrize("name,line", [
    ("comment", f"# {GUARD_RUN} && {DEPLOY}"),
    ("echo", f"echo {GUARD_RUN} && {DEPLOY}"),
    ("echoed name", f'echo "d1_write_' + f'guard.py" && npx {DEPLOY}'),
    ("string", f'MSG="{GUARD_RUN}" && npx {DEPLOY}'),
    ("wrong scope", f"python3 scripts/d1_write_" + f"guard.py --scope tools && {DEPLOY}"),
    ("no scope", f"python3 scripts/d1_write_" + f"guard.py && {DEPLOY}"),
    ("not chained", f"{GUARD_RUN}; {DEPLOY}"),
    ("failure ignored", f"{GUARD_RUN} || true && {DEPLOY}"),
    ("guard earlier, deploy later", f"{GUARD_RUN} && echo ok && {DEPLOY}"),
])
def test_deploy_needs_an_executed_guard_run(tmp_path, name, line):
    rc, out = _findings(tmp_path, "tools/deploy.sh", line + "\n", "tools")
    assert rc == 1 and " T5 " in out, (name, out)


@pytest.mark.parametrize("name,rel,text", [
    ("chained", "tools/d.sh", f"{GUARD_RUN} && {DEPLOY}\n"),
    ("chained npx", "tools/d.sh", f"{GUARD_RUN} && npx {DEPLOY}\n"),
    ("package.json", "tools/package.json",
     '{"scripts": {"deploy": "python3 ../scripts/d1_write_' + f'guard.py --scope all && {DEPLOY}"}}}}\n'),
    ("refuse-or-exit block", "tools/setup.sh",
     '    python3 "$DIR/../scripts/d1_write_' + 'guard.py" --scope all || { print_error "guard failed"; exit 1; }; '
     f"OUT=$(npx {DEPLOY} 2>&1)\n"),
])
def test_executed_guard_forms_are_clean(tmp_path, name, rel, text):
    rc, out = _findings(tmp_path, rel, text, "tools")
    assert rc == 0, (name, out)
