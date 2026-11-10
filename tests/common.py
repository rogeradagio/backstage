"""utilitats comunes de les proves."""


def setup_backend(env, **values):
    """Deixa la connexió de Backstage activa contra un Magento fictici (les crides es substitueixen a cada prova)."""
    backend = env["backstage.backend"]._get()
    backend.write({
        "enabled": True,
        "silent": False,
        "url": "http://magento.test",
        "token": "test-token",
        "connection_state": "ok",
        "stock_enabled": False,
        "delivered_enabled": False,
        **values,
    })
    return backend
