import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from bigshort.core import Config, Engine
from bigshort.hunter import (CANDIDATES, HunterLedger, OpenRouter, ROUND_MS,
                             execute_decision, manage_hunter, build_snapshot)
from bigshort.hunter_execution import HunterEngine, HunterStore
from bigshort.hunter_market import HunterMarket


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e=HunterEngine(Config())
        self.s=HunterStore(str(Path(self.tmp.name)/'paper.sqlite'),Config())
        self.api=Mock()
        self.api.now.return_value=120000
        self.api.get.return_value=[]

    def opened(self):
        self.assertTrue(self.e.enter('TEST','model',102,100,110000))

    def test_no_preentry_candle_execution(self):
        self.opened()
        self.e.observe(100,120000)
        self.assertIsNotNone(self.e.position)
        self.assertEqual(self.e.position.opened,110000)
        self.assertEqual(self.e.position.last_t,120000)

    def test_funding_failure_does_not_block_stop(self):
        self.opened()
        self.api.get.side_effect=RuntimeError('funding unavailable')
        with patch('bigshort.hunter.fresh_quote',return_value=(103,103.1,120000)):
            self.assertFalse(manage_hunter(self.api,self.e,self.s))
        self.assertIsNone(self.e.position)
        self.assertEqual(self.s.report()['closed_trades'],1)
        self.assertEqual(self.s.report()['funding_pending_closed_trades'],1)

    def test_stop_halt_persists_without_network(self):
        self.opened()
        self.api.now.side_effect=RuntimeError('offline')
        with patch('bigshort.hunter.fresh_quote',side_effect=RuntimeError('offline')):
            manage_hunter(self.api,self.e,self.s,stop=True)
        self.assertTrue(self.s.load()['halted'])
        self.assertIsNotNone(self.e.position)

    def test_deadline_exit(self):
        self.opened()
        with patch('bigshort.hunter.fresh_quote',return_value=(99,99.1,120000)):
            manage_hunter(self.api,self.e,self.s,expired=True)
        self.assertIsNone(self.e.position)
        self.assertTrue(self.e.halted)
        self.assertIn('tournament_deadline',str(self.s.report()['recent_events']))

    def test_delayed_funding_after_close_reconciles_once(self):
        self.opened();self.e.close(101,130000,'stop')
        before=self.e.cash
        self.s.save(self.e)
        self.api.get.return_value=[{'fundingTime':120000,'fundingRate':'.01','markPrice':'100'}]
        self.e.reconcile_funding(self.api,140000)
        self.s.save(self.e)
        after=self.e.cash
        self.e.reconcile_funding(self.api,150000)
        self.s.save(self.e)
        self.assertGreater(after,before)
        self.assertEqual(after,self.e.cash)
        self.assertAlmostEqual(self.s.report()['net_closed_pnl'],self.e.cash-100)
        self.assertEqual(self.s.report()['funding_pending_closed_trades'],0)

    def test_funding_while_open_not_double_counted_in_report(self):
        self.opened()
        self.api.get.return_value=[{'fundingTime':120000,'fundingRate':'.01','markPrice':'100'}]
        self.e.reconcile_funding(self.api,125000)
        self.e.close(101,130000,'stop')
        self.s.save(self.e)
        self.assertAlmostEqual(self.s.report()['net_closed_pnl'],self.e.cash-100)

    def test_legacy_position_is_preserved(self):
        old=Engine();old.enter('TEST','model',102,100,60000)
        new=HunterEngine(Config(),old.state())
        self.assertEqual(new.position.qty,old.position.qty)
        self.assertEqual(new.position.stop,old.position.stop)
        self.assertEqual(new.cash,old.cash)
        restored=HunterEngine(Config(),new.state())
        self.assertEqual(restored.settlements,new.settlements)

    def test_executable_quote_replaces_snapshot_price(self):
        snapshot=[{'market':{'symbol':'TEST','bid':100,'ask':100.01,'observed_at_ms':100000},
                   'item':{'step':.001,'minimum':5}}]
        decision={'action':'SHORT','symbol':'TEST','stop_pct':1,'confidence':.8}
        with patch('bigshort.hunter.fresh_quote',return_value=(100.2,100.21,120000)):
            action,executed=execute_decision(self.api,self.e,decision,snapshot,100000,None,900000)
        self.assertTrue(executed)
        self.assertAlmostEqual(self.e.position.entry,100.2*.999)
        self.assertEqual(self.e.position.opened,120000)

    def test_stale_decision_and_changed_position_rejected(self):
        with self.assertRaises(ValueError):
            execute_decision(self.api,self.e,{},[],0,None,100000)
        self.opened()
        with self.assertRaises(ValueError):
            execute_decision(self.api,self.e,{},[],100000,None,900000)

    def test_large_move_rejects_signal(self):
        snapshot=[{'market':{'symbol':'TEST','bid':100,'ask':100.01,'observed_at_ms':100000},
                   'item':{'step':.001,'minimum':5}}]
        decision={'action':'SHORT','symbol':'TEST','stop_pct':1,'confidence':.8}
        with patch('bigshort.hunter.fresh_quote',return_value=(98,98.01,120000)),self.assertRaises(ValueError):
            execute_decision(self.api,self.e,decision,snapshot,100000,None,900000)
        self.assertIsNone(self.e.position)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'ledger.sqlite')
        self.l=HunterLedger(self.path)
        self.c=replace(CANDIDATES[1],budget=3)

    def row(self):
        return dict(t=1,candidate=self.c.name,model=self.c.model,prompt='p',response=None,
                    decision=None,prompt_tokens=0,completion_tokens=0,cost=0,status='error',error='timeout')

    def test_unknown_cost_stays_reserved_after_restart(self):
        r=self.l.reserve(self.c,2)
        self.l.settle(r,self.row(),None)
        recovered=HunterLedger(self.path)
        self.assertEqual(recovered.committed(self.c.name),2)
        with self.assertRaises(RuntimeError): recovered.reserve(self.c,2)

    def test_crash_before_response_keeps_pending_reservation(self):
        self.l.reserve(self.c,2)
        self.assertEqual(HunterLedger(self.path).committed(),2)

    def test_actual_cost_releases_remainder(self):
        r=self.l.reserve(self.c,2)
        self.l.settle(r,self.row(),.2)
        self.assertAlmostEqual(self.l.committed(),.2)
        self.assertAlmostEqual(self.l.spent(),.2)

    def test_concurrent_reservations_cannot_exceed_cap(self):
        def attempt(_):
            try: self.l.reserve(self.c,2);return True
            except RuntimeError: return False
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(attempt,range(3)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.l.committed(),2)

    def test_free_agent_cannot_reserve_paid_usage(self):
        with self.assertRaises(RuntimeError): self.l.reserve(CANDIDATES[0],.01)

    def test_round_start_survives_restart_and_uses_original_calls(self):
        row=self.row();row['t']=100000
        self.l.save_call(**row)
        self.assertEqual(self.l.round_start(200000),100000)
        self.assertEqual(HunterLedger(self.path).round_start(300000),100000)

    def test_deadline_blocks_new_reservations(self):
        self.l.metadata('round_start_ms',int(time.time()*1000)-ROUND_MS-1)
        with self.assertRaises(RuntimeError): self.l.reserve(self.c,.01)

    def test_cost_overrun_latches_billing_halt(self):
        r=self.l.reserve(self.c,.1)
        self.l.settle(r,self.row(),.2)
        self.assertTrue(self.l.metadata('billing_halted'))
        with self.assertRaises(RuntimeError): self.l.reserve(self.c,.01)

    def response(self,obj): return io.BytesIO(json.dumps(obj).encode())

    def test_openrouter_reserves_and_bills_malformed_answer(self):
        catalog={'data':[{'id':self.c.model,'context_length':100000,
                         'pricing':{'prompt':str(self.c.prompt_price),'completion':str(self.c.completion_price)}}]}
        payload={'usage':{'cost':.00001,'prompt_tokens':10,'completion_tokens':10},
                 'choices':[{'message':{'content':'invalid json'}}]}
        responses=[self.response({'data':{'limit':10,'limit_reset':None}}),self.response(catalog),self.response(payload)]
        with patch('bigshort.hunter.urlopen',side_effect=responses),self.assertRaises(RuntimeError):
            OpenRouter('TEST_KEY',self.l).decide(self.c,'prompt',100000)
        self.assertAlmostEqual(self.l.spent(),.00001)
        self.assertAlmostEqual(self.l.committed(),.00001)

    def test_unlimited_key_blocks_completion_request(self):
        with patch('bigshort.hunter.urlopen',return_value=self.response({'data':{'limit':None}})) as request:
            with self.assertRaises(RuntimeError): OpenRouter('TEST_KEY',self.l).decide(self.c,'p',1)
        self.assertEqual(request.call_count,1)
        self.assertEqual(self.l.committed(),0)

    def test_provider_timeout_keeps_reservation(self):
        catalog={'data':[{'id':self.c.model,'context_length':100000,
                         'pricing':{'prompt':str(self.c.prompt_price),'completion':str(self.c.completion_price)}}]}
        responses=[self.response({'data':{'limit':10,'limit_reset':None}}),self.response(catalog),TimeoutError()]
        with patch('bigshort.hunter.urlopen',side_effect=responses),self.assertRaises(RuntimeError):
            OpenRouter('TEST_KEY',self.l).decide(self.c,'prompt',100000)
        self.assertGreater(self.l.committed(),0)
        self.assertEqual(self.l.spent(),0)


class MarketTests(unittest.TestCase):
    def test_ban_cooldown_survives_restart(self):
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as d:
            api=HunterMarket(d)
            error=HTTPError('url',429,'rate limited',{'Retry-After':'120'},None)
            with patch('bigshort.hunter_market.urlopen',side_effect=error),self.assertRaises(RuntimeError):
                api.get('time')
            recovered=HunterMarket(d)
            with patch('bigshort.hunter_market.urlopen') as request,self.assertRaises(RuntimeError):
                recovered.get('time')
            request.assert_not_called()

    def test_stale_snapshot_excluded(self):
        from bigshort.core import Bar
        api=Mock();api.now.return_value=999000000
        api.universe.return_value=[{'symbol':'TEST'}]
        api.bars.return_value=[Bar(i*60000,100,101,99,100,1) for i in range(1,22)]
        with patch('bigshort.hunter.fresh_quote',return_value=(100,100.01,999000000)):
            self.assertEqual(build_snapshot(api,Config(),999000000),[])


class AdditionalSafetyTests(unittest.TestCase):
    def test_partial_legacy_quantity_reconstructed(self):
        original=Engine();original.enter('TEST','model',102,100,60000)
        original.close(90,120000,'partial',.5)
        original.position.partial=True
        imported=HunterEngine(Config(),original.state(),original.events)
        self.assertAlmostEqual(imported.settlements[0]['exposure'][0]['qty'],original.position.initial_qty)
        self.assertAlmostEqual(imported.settlements[0]['exposure'][-1]['qty'],original.position.qty)

    def test_duplicate_settlement_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=HunterLedger(str(Path(d)/'s.sqlite'))
            row=dict(t=1,candidate='analyst',model='m',prompt='p',response=None,decision=None,
                     prompt_tokens=0,completion_tokens=0,cost=0,status='error',error='x')
            reservation=ledger.reserve(CANDIDATES[1],.1)
            ledger.settle(reservation,row,.05)
            with self.assertRaises(ValueError): ledger.settle(reservation,row,.05)
            self.assertAlmostEqual(ledger.spent(),.05)

    def test_restart_model_schedule_does_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'s.sqlite');ledger=HunterLedger(path)
            ledger.metadata('next_decision_ms',12345)
            self.assertEqual(HunterLedger(path).metadata('next_decision_ms'),12345)


if __name__=='__main__': unittest.main()
