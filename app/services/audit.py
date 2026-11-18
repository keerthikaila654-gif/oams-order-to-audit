import json


def log(conn, user, action, entity, entity_id=None, old=None, new=None, ref=None):
    """Append an audit record. `user` is a dict/Row with id & username, or None (system)."""
    uid = user["id"] if user else None
    uname = user["username"] if user else "system"
    conn.execute(
        "INSERT INTO audit_logs(user_id,username,action,entity,entity_id,ref,old_json,new_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uid, uname, action, entity, entity_id, ref,
         json.dumps(old, default=str) if old is not None else None,
         json.dumps(new, default=str) if new is not None else None))
