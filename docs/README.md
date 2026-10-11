# 文档入口与优先级

1. [当前产品契约](current-product-contract.md) 是唯一能够定义产品范围、数据准入、价格口径与捡漏结论的文件。
2. [产品概述](product-overview.md)、[需求](requirements.md)、[来源](sources.md)、[实施 AC](core-implementation-ac.md)、[当前验收](core-acceptance.md) 和 [运行环境](environment.md) 只能解释或验证该契约。
3. 本目录其余专题文档只能作为实现参考，不能推翻当前产品契约。
4. [history/](history/README.md) 是只读审计归档；其中的旧接口结论、渠道清单、PID 和测试结果一律不代表当前状态。

当页面、代码、专题文档与当前产品契约不一致时，先以当日真实 API 回放、数据库快照和自动化测试查证，然后同步修正契约、实现和验收。
