"""HTTP routes grouped by workbench business domain."""

def register_all(app):
    from routes import benefits, ledger, notifications, opportunities, price_benchmarks, search, sources, system, xianyu

    for module in (system, search, benefits, sources, xianyu, opportunities, ledger, price_benchmarks, notifications):
        module.register(app)
