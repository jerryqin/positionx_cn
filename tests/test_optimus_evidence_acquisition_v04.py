import importlib.util
import pathlib
import sys
import unittest

P = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_evidence_acquisition_v04.py"
spec = importlib.util.spec_from_file_location("optimus_evidence_v04_testmod", P)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class EvidenceAcquisitionV04Tests(unittest.TestCase):
    def test_symbol_scope(self):
        self.assertEqual(mod.symbol_from_cninfo("002050"), "002050.SZ")
        self.assertEqual(mod.symbol_from_cninfo("688017"), "688017.SH")
        self.assertIsNone(mod.symbol_from_cninfo("03752"))
        self.assertIsNone(mod.symbol_from_cninfo("830001"))

    def test_component_screw(self):
        self.assertIn("roller screws", mod.component_domain("人形机器人行星滚柱丝杠已送样"))

    def test_level_designation(self):
        self.assertGreaterEqual(mod.max_evidence_level("机器人客户项目已经获得定点"), 4)

    def test_level_order(self):
        self.assertGreaterEqual(mod.max_evidence_level("公司获得人形机器人执行器采购订单"), 7)

    def test_level_recurring_revenue(self):
        self.assertEqual(mod.max_evidence_level("机器人零部件批量交付并形成销售收入"), 9)

    def test_explicit_tesla_link(self):
        t = "公司与特斯拉 Optimus 项目合作，执行器已经获得定点"
        level = mod.max_evidence_level(t)
        self.assertEqual(mod.tesla_link_level(t, level), 4)

    def test_unnamed_head_link(self):
        t = "人形机器人项目已向海外头部客户送样验证"
        self.assertEqual(mod.tesla_link_level(t, 5), 2)

    def test_hard_negative(self):
        k, s = mod.contradiction("公司明确表示未向特斯拉 Optimus 供货")
        self.assertEqual(k, "SUPPLIER_OR_ORDER_DENIAL")
        self.assertEqual(s, "HARD")

    def test_soft_negative(self):
        k, s = mod.contradiction("相关产品尚未形成收入，目前处于样品阶段")
        self.assertEqual(s, "SOFT")

    def test_stable_evidence_id(self):
        x = {"a": 1, "b": [2, 3]}
        self.assertEqual(mod.stable_id("E", x), mod.stable_id("E", x))

    def test_snippets(self):
        txt = "前文" * 100 + "人形机器人执行器获得定点并进入验证" + "后文" * 100
        ss = mod.snippets_for(txt, ["人形机器人", "定点"], 40, 3)
        self.assertTrue(ss)
        self.assertTrue(any("定点" in x for x in ss))

    def test_provisional_policy_is_explicit(self):
        cfg = mod.load_json(pathlib.Path(__file__).resolve().parents[1] / "config/research/optimus_evidence_acquisition_v0_4.json")
        self.assertEqual(cfg["policy"]["initial_review_state"], "PROVISIONAL")
        self.assertFalse(cfg["policy"]["auto_canonical_upgrade"])


if __name__ == "__main__":
    unittest.main()

class EvidenceAcquisitionV041Tests(unittest.TestCase):
    def test_seed_relevance_rejects_unrelated_text(self):
        self.assertFalse(mod.seed_text_is_relevant("2026年半年度报告 公司治理 财务报表"))

    def test_seed_relevance_accepts_robot_body_text(self):
        self.assertTrue(mod.seed_text_is_relevant("公司人形机器人执行器已进入客户验证阶段"))

    def test_seed_relevance_accepts_component_body_text(self):
        self.assertTrue(mod.seed_text_is_relevant("行星滚柱丝杠项目处于送样阶段"))

    def test_cninfo_post_default_keyword_is_empty(self):
        import inspect
        sig = inspect.signature(mod.cninfo_post)
        self.assertEqual(sig.parameters["searchkey"].default, "")


class EvidenceAcquisitionV042ContextTests(unittest.TestCase):
    def test_unrelated_company_revenue_does_not_become_l9(self):
        txt = (
            "公司正在拓展仿生机器人机电执行器业务。"
            "公司2026年上半年营业收入169亿元，同比增长3.9%。"
        )
        self.assertLess(mod.max_evidence_level(txt, "2026年半年度报告"), 9)

    def test_governance_contract_language_cannot_create_l7(self):
        txt = (
            "经营范围包括智能机器人的研发。"
            "公司与银行签订募集资金专户存储三方监管协议。"
        )
        self.assertLessEqual(mod.max_evidence_level(txt, "公司章程"), 2)

    def test_robot_batch_delivery_is_l9(self):
        txt = "公司具身机器人电机转子已实现小批量产品交付，并持续推进产能建设。"
        self.assertEqual(mod.max_evidence_level(txt, "2026年半年度报告"), 9)

    def test_robot_order_is_l7(self):
        txt = "公司人形机器人执行器已获得客户采购订单。"
        self.assertGreaterEqual(mod.max_evidence_level(txt, "投资者关系活动记录表"), 7)

    def test_named_tesla_link_is_context_scoped(self):
        txt = (
            "公司汽车业务客户包括Tesla。"
            + "其他业务介绍。" * 120
            + "公司具身机器人业务仍处于早期研发阶段。"
        )
        level = mod.max_evidence_level(txt, "2026年半年度报告")
        self.assertLess(mod.tesla_link_level(txt, level), 4)

    def test_context_classifier_returns_basis(self):
        txt = "人形机器人执行器进入客户验证阶段。"
        level, rule, basis = mod.classify_evidence_level(txt, "投资者关系活动记录表")
        self.assertGreaterEqual(level, 5)
        self.assertTrue(rule)
        self.assertIn("验证", basis)


class EvidenceAcquisitionV043MilestoneStrictTests(unittest.TestCase):
    def test_delivery_capability_is_not_l9(self):
        txt = (
            "公司持续聚焦具身机器人等高成长赛道，深耕微型传动、电机及控制系统，"
            "不断增强产品平台化开发能力和规模化交付能力。"
        )
        self.assertLess(mod.max_evidence_level(txt, "2026年半年度报告"), 9)

    def test_industry_mass_production_forecast_is_not_l8(self):
        txt = (
            "具身智能产业生态持续完善，商业化落地进程提速，工业协作机器人、"
            "人形服务机器人逐步完成场景验证、小批量试产，向规模化量产阶段过渡。"
        )
        self.assertLess(mod.max_evidence_level(txt, "2026年半年度报告"), 8)

    def test_actual_robot_batch_delivery_is_l9(self):
        txt = "目前，公司仿生机器人机电执行器核心产品稳步推进批量交付与产线爬坡。"
        self.assertEqual(mod.max_evidence_level(txt, "投资者关系活动记录表"), 9)

    def test_robot_order_growth_is_l7_not_l9(self):
        txt = "报告期内公司产能大幅爬升，受机器人业务订单规模增长带动，产能利用率持续改善。"
        self.assertEqual(mod.max_evidence_level(txt, "2026年半年度报告"), 7)

    def test_robot_parts_revenue_is_l9(self):
        txt = "报告期内，机器人相关零部件业务收入1,784万元，同比增长115.51%。"
        self.assertEqual(mod.max_evidence_level(txt, "2026年半年度报告"), 9)

    def test_embodied_business_shipments_are_l9(self):
        txt = "公司具身智能业务继续高速增长，累计出货量位居行业前列，相关产品批量交付贡献收入增量。"
        self.assertEqual(mod.max_evidence_level(txt, "投资者关系活动记录表"), 9)

    def test_tesla_in_investor_question_plus_denial_is_not_link(self):
        txt = (
            "Q6: 网络传闻特斯拉拟收购公司或与公司合作开发人形机器人，是否属实？"
            "A: 关于‘特斯拉收购公司’，该传闻不属实，公司未筹划、商谈或接触此类收购事项。"
            "公司机器人核心部件业务进入交付放量期。"
        )
        lvl = mod.max_evidence_level(txt, "投资者关系活动记录表")
        self.assertEqual(lvl, 9)
        self.assertEqual(mod.tesla_link_level(txt, lvl), 0)
        self.assertEqual(mod.contradiction(txt)[0], "NAMED_TESLA_RUMOR_DENIAL")

    def test_affirmative_named_tesla_link_remains_t4(self):
        txt = "公司向特斯拉Optimus项目供应执行器，已获得客户定点并进入验证阶段。"
        lvl = mod.max_evidence_level(txt, "投资者关系活动记录表")
        self.assertGreaterEqual(lvl, 4)
        self.assertEqual(mod.tesla_link_level(txt, lvl), 4)

    def test_unnamed_world_known_tech_company_is_t2(self):
        txt = "报告期内，公司已获得世界知名科技公司具身机器人电机转子项目定点，正推进量产准备。"
        lvl = mod.max_evidence_level(txt, "2026年半年度报告")
        self.assertGreaterEqual(lvl, 4)
        self.assertEqual(mod.tesla_link_level(txt, lvl), 2)

class EvidenceAcquisitionV044ContextQualityTests(unittest.TestCase):
    def test_industry_function_validation_is_not_l5(self):
        txt = (
            "2026年被业界公认为人形机器人量产兑现元年，产业正从功能验证向成本与性能均衡切换。"
            "公司持续推动微型传动系统在灵巧手和机器人关节中的应用突破。"
        )
        self.assertLess(mod.max_evidence_level(txt, "2026年半年度报告"), 5)

    def test_company_customer_validation_is_l5(self):
        txt = "报告期内公司人形机器人关节产品已向客户送样，并进入客户验证阶段。"
        self.assertEqual(mod.max_evidence_level(txt, "投资者关系活动记录表"), 5)

    def test_conditional_overseas_head_customer_is_not_t2(self):
        txt = (
            "人形机器人业务进入交付放量期。"
            "未来若海外头部客户订单达到经济规模，公司将综合评估海外建厂可行性。"
        )
        lvl = mod.max_evidence_level(txt, "投资者关系活动记录表")
        self.assertEqual(mod.tesla_link_level(txt, lvl), 0)

    def test_existing_world_known_company_designation_remains_t2(self):
        txt = "报告期内，公司已获得世界知名科技公司具身机器人电机转子项目定点，正推进量产准备。"
        lvl = mod.max_evidence_level(txt, "2026年半年度报告")
        self.assertEqual(mod.tesla_link_level(txt, lvl), 2)

    def test_related_party_name_list_only_is_not_l1(self):
        txt = (
            "宁波均普人工智能与人形机器人研究院有限公司 受同一最终控制方控制；"
            "宁波具身智能机器人创新中心有限公司 受同一最终控制方控制。"
        )
        self.assertEqual(mod.max_evidence_level(txt, "2026年半年度报告"), 0)


class EvidenceAcquisitionV045DebtBackfillTests(unittest.TestCase):
    def test_date_range_inclusive(self):
        ds = mod.date_range_inclusive("2026-08-31", "2026-09-02")
        self.assertEqual([d.isoformat() for d in ds], ["2026-08-31", "2026-09-01", "2026-09-02"])

    def test_date_range_rejects_reverse(self):
        with self.assertRaises(ValueError):
            mod.date_range_inclusive("2026-09-02", "2026-08-31")

    def test_capture_routes_seed_debt_to_all_announcements(self):
        import inspect
        src = inspect.getsource(mod.capture)
        self.assertIn('seed_debt_backfill', src)
        self.assertIn('query_seed_all_announcements', src)

    def test_status_surfaces_query_stats(self):
        import inspect
        src = inspect.getsource(mod.latest_manifest)
        self.assertIn('query_stats', src)
        self.assertIn('query_quality', src)
