import unittest
import tempfile
import json
from pathlib import Path
from bigshort.hunter import HunterLedger, sync_candidate_models
from bigshort.hunter import validate_key_metadata, model_rates, CANDIDATES, LEGACY_OPUS


class CompatibilityTests(unittest.TestCase):
    def test_ten_dollar_key_with_future_expiration(self):
        data={'limit':10,'limit_reset':None,'usage':.05,'limit_remaining':9.95,
              'expires_at':'2026-09-18T00:00:00Z'}
        self.assertEqual(validate_key_metadata(data,1789603200)['available'],9.95)

    def test_reset_does_not_restore_lifetime_allowance(self):
        data={'limit':10,'limit_reset':'daily','usage':8,'limit_remaining':10}
        self.assertEqual(validate_key_metadata(data)['available'],2)

    def test_reset_requires_usage(self):
        with self.assertRaises(RuntimeError): validate_key_metadata({'limit':10,'limit_reset':'daily'})

    def test_expired_key_rejected(self):
        with self.assertRaises(RuntimeError):
            validate_key_metadata({'limit':10,'expires_at':'2020-01-01T00:00:00Z'})

    def test_overbudget_or_unlimited_key_rejected(self):
        for limit in (None,30,float('nan')):
            with self.assertRaises(RuntimeError): validate_key_metadata({'limit':limit})

    def test_opus_catalog_optional_web_and_cache_fields(self):
        # Relevant public-catalog fields fetched on 2026-09-17.
        model={'pricing':{'prompt':'0.000005','completion':'0.000025','web_search':'0.01',
                          'input_cache_read':'0.0000005','input_cache_write':'0.00000625',
                          'input_cache_write_1h':'0.00001'}}
        self.assertEqual(model_rates(model,LEGACY_OPUS,16000),.00001)

    def test_unreachable_tier_does_not_block_short_prompt(self):
        model={'pricing':{'prompt':'0.000005','completion':'0.000025',
                          'overrides':[{'min_prompt_tokens':100000,'prompt':'0.00001','completion':'0.00005'}]}}
        self.assertEqual(model_rates(model,LEGACY_OPUS,16000),.000005)
        with self.assertRaises(ValueError): model_rates(model,LEGACY_OPUS,120000)

    def test_unrecognized_active_charge_still_blocks(self):
        with self.assertRaises(ValueError):
            model_rates({'pricing':{'prompt':0,'completion':0,'unknown_surcharge':1}},CANDIDATES[0],1000)

    def test_cache_fee_cannot_silently_make_scout_paid(self):
        # The caller reserves this amount; Scout's zero-dollar budget then rejects it.
        self.assertGreater(model_rates({'pricing':{'prompt':0,'completion':0,'input_cache_write_1h':.01}},
                                       CANDIDATES[0],1000),0)


class ReplacementTests(unittest.TestCase):
    def test_swap_preserves_spending_and_deadline_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=HunterLedger(str(Path(d)/'ledger.sqlite'))
            old=[{'name':c.name,'model':LEGACY_OPUS.model if c.name=='elite' else c.model} for c in CANDIDATES]
            ledger.metadata('candidate_models',old)
            ledger.metadata('round_start_ms',123456)
            ledger.save_call(t=123456,candidate='elite',model=LEGACY_OPUS.model,prompt='p',response=None,
                             decision='{"action":"HOLD"}',prompt_tokens=1,completion_tokens=1,cost=.05,status='ok',error=None)
            sync_candidate_models(ledger)
            sync_candidate_models(ledger)
            self.assertEqual(ledger.metadata('candidate_models')[-1]['model'],'z-ai/glm-5.3')
            self.assertEqual(len(ledger.metadata('model_transitions')),1)
            self.assertEqual(ledger.spent('elite'),.05)
            self.assertEqual(ledger.metadata('round_start_ms'),123456)
            self.assertEqual(ledger.recent_decisions('elite',model='z-ai/glm-5.3'),[])

    def test_unapproved_model_change_still_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=HunterLedger(str(Path(d)/'ledger.sqlite'))
            old=[{'name':c.name,'model':'other' if c.name=='elite' else c.model} for c in CANDIDATES]
            ledger.metadata('candidate_models',old)
            with self.assertRaises(ValueError): sync_candidate_models(ledger)

    def test_glm_prices_fit_configured_ceiling(self):
        model={'pricing':{'prompt':'0.0000014','completion':'0.0000044','input_cache_read':'0.00000026'}}
        self.assertEqual(CANDIDATES[2].model,'z-ai/glm-5.3')
        self.assertEqual(CANDIDATES[2].max_tokens,2048)
        self.assertEqual(model_rates(model,CANDIDATES[2],16000),.0000014)


if __name__=='__main__': unittest.main()
