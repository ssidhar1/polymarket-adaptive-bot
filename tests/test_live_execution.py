from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from src.execution.live import (DuplicateLiveIntent, LiveExecutionDisabled, LiveExecutionError,
                                LiveExecutor, LiveSubmissionUnknown, PER_ORDER_APPROVAL)
ASSET = "12345678901234567890"
class Page:
    def __init__(self, items=()): self.items=list(items)
class Paginator:
    def first_page(self): return Page()
class FakeClient:
    def __init__(self, age=0, allowance=2_000_000_000, fail=False):
        self.book=SimpleNamespace(timestamp=datetime.now(timezone.utc)-timedelta(seconds=age),
            bids=[SimpleNamespace(price="0.49",size="100")],
            asks=[SimpleNamespace(price="0.51",size="100")],min_order_size="1")
        self.allowance=allowance; self.fail=fail; self.posts=0; self.created=None
    def get_order_book(self, asset_id): return self.book
    def list_open_orders(self): return Paginator()
    def list_positions(self, status):
        assert status == "OPEN"
        return Paginator()
    def get_balance_allowance(self, asset_type):
        return SimpleNamespace(balance=2_000_000_000,allowances={"exchange":self.allowance})
    def create_limit_order(self, **kwargs): self.created=kwargs; return object()
    def post_order(self, signed):
        self.posts+=1
        if self.fail: raise TimeoutError("unknown network outcome")
        return SimpleNamespace(ok=True,status="live",order_id="oid-1",trade_ids=())
def config():
    return {"live_execution":{"enabled":True,"max_order_notional_usd":10,"max_position_pct":.01,
        "max_total_exposure_pct":.05,"max_open_orders":1,"max_book_age_seconds":3,
        "max_spread":.05,"order_ttl_seconds":600}}
def env():
    return {"ASTF_ENABLE_LIVE_TRADING":"YES","ASTF_LIVE_CONFIRMATION":"I_ACCEPT_REAL_MONEY_RISK","DEFAULT_MODE":"live"}
def make(tmp_path, client=None):
    return LiveExecutor(client or FakeClient(),config(),tmp_path/"orders.sqlite",environ=env())
def submit(ex, **kw):
    args={"intent_key":"intent-0001","asset_id":ASSET,"price":".50","size":"10",
          "bankroll_usd":"1000","risk_paused":False,"approval":PER_ORDER_APPROVAL}
    args.update(kw); return ex.submit_buy_limit(**args)
def test_disabled_in_paper_mode(tmp_path):
    e=env();e["DEFAULT_MODE"]="paper"
    with pytest.raises(LiveExecutionDisabled): LiveExecutor(FakeClient(),config(),tmp_path/"x",environ=e)
def test_requires_per_order_approval(tmp_path):
    with pytest.raises(LiveExecutionDisabled): submit(make(tmp_path),approval="")
def test_respects_risk_pause(tmp_path):
    with pytest.raises(LiveExecutionError): submit(make(tmp_path),risk_paused=True)
def test_post_only_and_journaled(tmp_path):
    c=FakeClient(); ex=make(tmp_path,c); out=submit(ex)
    assert out["ok"] and out["order_id"]=="oid-1" and c.created["post_only"] is True
    with ex._db() as db: row=db.execute("select status,order_id from live_order_intents").fetchone()
    assert row==("ACCEPTED","oid-1")
def test_duplicate_intent_never_posts_twice(tmp_path):
    c=FakeClient(); ex=make(tmp_path,c); submit(ex)
    with pytest.raises(DuplicateLiveIntent): submit(ex)
    assert c.posts==1
def test_stale_book_fails_closed(tmp_path):
    c=FakeClient(age=30); ex=make(tmp_path,c)
    with pytest.raises(LiveExecutionError,match="Stale"): submit(ex)
    assert c.posts==0
def test_insufficient_allowance_does_not_auto_approve(tmp_path):
    c=FakeClient(allowance=1); ex=make(tmp_path,c)
    with pytest.raises(LiveExecutionError,match="allowance"): submit(ex)
    assert c.posts==0
def test_ambiguous_submit_is_quarantined(tmp_path):
    c=FakeClient(fail=True); ex=make(tmp_path,c)
    with pytest.raises(LiveSubmissionUnknown): submit(ex)
    c.fail=False
    with pytest.raises(DuplicateLiveIntent): submit(ex)
    assert c.posts==1

def test_existing_positions_count_toward_portfolio_cap(tmp_path):
    client = FakeClient()
    class PositionPaginator:
        def first_page(self):
            return Page([SimpleNamespace(current_value="45", total_cost_usdc="45")])
    client.list_positions = lambda status: PositionPaginator()
    executor = make(tmp_path, client)
    with pytest.raises(LiveExecutionError, match="Portfolio exposure"):
        submit(executor)
    assert client.posts == 0
