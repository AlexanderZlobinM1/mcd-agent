from __future__ import annotations

from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.models import MauticInstall


def _select_instance(cfg: AgentConfig, root: str | None) -> MauticInstall:
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    if root:
        for inst in installs:
            if inst.root == root or inst.instance_uid == root:
                return inst
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0]


def reset_admin_password(
    cfg: AgentConfig,
    *,
    root: str | None,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    password_hash: str,
) -> dict[str, Any]:
    inst = _select_instance(cfg, root)
    if inst.db is None:
        raise RuntimeError(f"Database credentials not found for instance: {inst.root}")

    username_clean = str(username or "").strip()
    email_clean = str(email or "").strip()
    first_name_clean = str(first_name or "").strip()
    last_name_clean = str(last_name or "").strip()
    password_hash_clean = str(password_hash or "").strip()
    if not username_clean:
        raise RuntimeError("username is required")
    if not email_clean:
        raise RuntimeError("email is required")
    if not password_hash_clean:
        raise RuntimeError("password_hash is required")

    prefix = str(inst.db.table_prefix or "")
    users_table = f"`{prefix}users`"
    roles_table = f"`{prefix}roles`"
    db = MauticDB(inst.db)

    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT `id` FROM {roles_table} WHERE `is_admin`=1 ORDER BY `id` ASC LIMIT 1")
            role_row = cur.fetchone() or {}
            role_id = int(role_row.get("id") or 0)
            if role_id <= 0:
                raise RuntimeError("admin role not found")

            cur.execute(
                f"SELECT `id`,`timezone`,`locale`,`date_added` "
                f"FROM {users_table} "
                f"WHERE `username`=%s OR `email`=%s "
                f"ORDER BY `id` ASC",
                (username_clean, email_clean),
            )
            matches = list(cur.fetchall() or [])
            keep_row = matches[0] if matches else None
            keep_id = int((keep_row or {}).get("id") or 0)

            if len(matches) > 1:
                dup_ids = [int((r or {}).get("id") or 0) for r in matches[1:]]
                dup_ids = [x for x in dup_ids if x > 0]
                if dup_ids:
                    placeholders = ",".join(["%s"] * len(dup_ids))
                    cur.execute(f"DELETE FROM {users_table} WHERE `id` IN ({placeholders})", dup_ids)

            timezone = str((keep_row or {}).get("timezone") or "UTC").strip() or "UTC"
            locale = str((keep_row or {}).get("locale") or "en_US").strip() or "en_US"
            if keep_id > 0:
                cur.execute(
                    f"UPDATE {users_table} "
                    f"SET `role_id`=%s, `username`=%s, `password`=%s, `first_name`=%s, `last_name`=%s, "
                    f"`email`=%s, `timezone`=%s, `locale`=%s, `is_published`=1, `last_login`=NULL "
                    f"WHERE `id`=%s",
                    (
                        role_id,
                        username_clean,
                        password_hash_clean,
                        first_name_clean,
                        last_name_clean,
                        email_clean,
                        timezone,
                        locale,
                        keep_id,
                    ),
                )
                action = "updated"
                user_id = keep_id
            else:
                cur.execute(
                    f"INSERT INTO {users_table} "
                    f"(`role_id`,`username`,`password`,`first_name`,`last_name`,`email`,`timezone`,`locale`,`is_published`,`date_added`,`last_login`) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,NOW(),NULL)",
                    (
                        role_id,
                        username_clean,
                        password_hash_clean,
                        first_name_clean,
                        last_name_clean,
                        email_clean,
                        "UTC",
                        "en_US",
                    ),
                )
                action = "inserted"
                user_id = int(cur.lastrowid or 0)

            cur.execute(
                f"SELECT `id`,`username`,`email`,`role_id`,`is_published` "
                f"FROM {users_table} WHERE `id`=%s",
                (user_id,),
            )
            row = cur.fetchone() or {}

    return {
        "status": "ok",
        "action": action,
        "instance": inst.instance_uid,
        "root": inst.root,
        "db_name": inst.db.name,
        "table_prefix": prefix,
        "user": {
            "id": int((row or {}).get("id") or 0),
            "username": str((row or {}).get("username") or ""),
            "email": str((row or {}).get("email") or ""),
            "role_id": int((row or {}).get("role_id") or 0),
            "is_published": int((row or {}).get("is_published") or 0),
        },
    }
