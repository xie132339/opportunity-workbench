"""Keep active documentation from silently reverting to superseded product rules."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "product-overview.md",
    ROOT / "docs" / "requirements.md",
    ROOT / "docs" / "sources.md",
    ROOT / "docs" / "environment.md",
    ROOT / "docs" / "core-acceptance.md",
    ROOT / "docs" / "core-implementation-ac.md",
)


class CurrentProductContractDocumentationTests(unittest.TestCase):
    def test_active_documents_all_point_to_the_current_contract(self):
        for document in ACTIVE_DOCS:
            with self.subTest(document=document.name):
                self.assertIn("current-product-contract.md", document.read_text(encoding="utf-8"))

    def test_contract_preserves_the_official_promotion_admission_boundary(self):
        contract = (ROOT / "docs" / "current-product-contract.md").read_text(encoding="utf-8")
        self.assertIn("SKU 详情是增强证据，不是官方优惠展示的阻断条件", contract)
        self.assertIn("taobao.tbk.dg.material.optional.upgrade", contract)
        self.assertIn("按维护基准：可捡漏", contract)
        self.assertIn("本工作台内维护的具体商品价格带", contract)

    def test_history_is_explicitly_excluded_from_current_decisions(self):
        archive_index = (ROOT / "docs" / "history" / "README.md").read_text(encoding="utf-8")
        self.assertIn("不得从本目录推导当前产品状态", archive_index)


if __name__ == "__main__":
    unittest.main()
