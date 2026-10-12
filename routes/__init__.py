"""HTTP routes grouped by workbench business domain."""

def register_all(app):
    from routes import (benefits, festival_promotions, ledger, notifications,
                        opportunities, price_benchmarks, search, sources, system,
                        taobao_campaigns, xianyu)

    for module in (system, search, benefits, sources, festival_promotions,
                   taobao_campaigns, xianyu, opportunities, ledger,
                   price_benchmarks, notifications):
        module.register(app)
