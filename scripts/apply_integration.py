#!/usr/bin/env python3
"""Apply calibrator + complete-set integration to src/main.py"""
from pathlib import Path

p = Path("src/main.py")
t = p.read_text()

if "CalibratedProbabilityModel" not in t:
    t = t.replace(
        "from src.learning.adaptive import AdaptiveLearner",
        "from src.learning.adaptive import AdaptiveLearner\nfrom src.learning.calibrated import CalibratedProbabilityModel"
    )
    t = t.replace(
        "    learner = AdaptiveLearner(config)",
        "    learner = AdaptiveLearner(config)\n    calibrator = CalibratedProbabilityModel(min_samples=15)"
    )
    t = t.replace(
        "session_resolves += 1\n                                session_pnl += float(trade[\"pnl\"])\n                                if float(trade[\"pnl\"]) > 0:\n                                    session_wins += 1",
        "session_resolves += 1\n                                session_pnl += float(trade[\"pnl\"])\n                                if float(trade[\"pnl\"]) > 0:\n                                    session_wins += 1\n                                calibrator.observe(trade)",
        1
    )
    t = t.replace(
        'sig_up = ensemble.directional_score(feat_up, preferred_side="up")',
        'cal_prob_up = calibrator.calibrate(0.52, datetime.now(timezone.utc).hour, float(feat_up.get("time_remaining_s") or 300), vs_open)\n                        sig_up = ensemble.directional_score(feat_up, preferred_side="up", calibrated_prob=cal_prob_up)'
    )
    t = t.replace(
        'sig_dn = ensemble.directional_score(feat_dn, preferred_side="down")',
        'cal_prob_dn = calibrator.calibrate(0.52, datetime.now(timezone.utc).hour, float(feat_dn.get("time_remaining_s") or 300), vs_open)\n                        sig_dn = ensemble.directional_score(feat_dn, preferred_side="down", calibrated_prob=cal_prob_dn)'
    )
    print("Calibrator wired")

old = '''                        if arb_watch:
                            logger.info(
                                f"ARB WATCH (execution disabled) | estimated_edge={arb_watch['edge']:.3f} "
                                f"| {slug[:40]}"
                            )
'''
new = '''                        if arb_watch and config.get("execution", {}).get("execute_complete_set", False):
                            edge = float(arb_watch.get("edge") or 0)
                            if edge > 0.003:
                                up_px = float(arb_watch["up_price"])
                                dn_px = float(arb_watch["down_price"])
                                pair = up_px + dn_px
                                if 0.9 < pair < 0.998:
                                    budget = min(float(risk.bankroll) * 0.025, 35.0)
                                    n = budget / pair if pair else 0
                                    su, sd = n * up_px, n * dn_px
                                    if su > 0.5 and sd > 0.5 and risk.can_open(su + sd, open_count=open_n):
                                        meta = _timing_meta(m, feat_up, 0.95, edge, mom, regime, "arb")
                                        ok = 0
                                        for side, tok, px, sz in [("up", up_token, up_px, su), ("down", down_token, dn_px, sd)]:
                                            ord = executor.place_order(tok, "buy", px, sz, mid_id, already_aggressive=True)
                                            if ord:
                                                tracker.add(ord, direction=side, slug=slug, end_date=m.get("endDate") or m.get("end_date_iso"), meta=meta)
                                                open_n += 1
                                                ok += 1
                                        if ok == 2:
                                            cooldown_until[mid_id] = now + cooldown_sec
                                            trades_this_cycle += 1
                                            logger.info(f"ARB EXECUTE | edge={edge:.3f} | spend=${su+sd:.1f} | {slug[:32]}")
                                            continue
                        if arb_watch:
                            logger.info(f"ARB WATCH | edge={arb_watch.get('edge',0):.3f} | {slug[:40]}")
'''
if old in t and "ARB EXECUTE" not in t:
    t = t.replace(old, new, 1)
    print("Complete-set execution wired")

p.write_text(t)
print("Done. Lines:", len(t.splitlines()))
