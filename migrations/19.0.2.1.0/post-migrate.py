"""L'estat únic (Aturat / Prova / Actiu) passa a dos interruptors: enabled i silent."""


def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'backstage_backend' AND column_name = 'state'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE backstage_backend
           SET enabled = state IN ('test', 'active'),
               silent = state <> 'active'
    """)
    cr.execute("ALTER TABLE backstage_backend DROP COLUMN state")
