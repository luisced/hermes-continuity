"""Retain usable questions; revalidate cited records rather than whole packets."""
import json
from pathlib import Path
from typing import Any
import unittest
from types import SimpleNamespace
from tests import test_checkin as fixtures
from continuity.checkin import Checkins

NOW=fixtures.NOW
class DraftRevalidation(unittest.TestCase):
    s: fixtures.Store
    model: Any
    calls: list
    sent: list
    item: dict
    tmp: Any
    setUp=fixtures.CheckinTests.setUp
    transport=fixtures.CheckinTests.transport

    def engine(self, clock):
        return Checkins(self.s,self.model,clock=lambda:clock[0],transport=self.transport)

    def test_unrelated_ledger_addition_during_synthesis_does_not_discard_question(self):
        original=self.model
        def complete(**kw):
            result=original.complete_structured(**kw)
            self.s.create_item('commitment','fixture','unrelated invoice','other','unrelated invoice')
            return result
        self.model=SimpleNamespace(complete_structured=complete)
        self.assertEqual(self.engine([NOW]).tick(NOW),1)
        self.assertEqual(len(self.calls),1)

    def test_uncited_external_source_change_during_synthesis_retains_question(self):
        from continuity.adapters import AdapterRegistry, MarkdownAdapter
        root=Path(self.tmp.name); note=root/'mail.md'; note.write_text('An unrelated email')
        self.s.configure('adapters',[{'type':'markdown','role':'mail','scope':'selected','root':str(root)}])
        config=self.s.setting('checkin',{}); config['sources']={'email':[{'role':'mail','scope':'selected','path':'mail.md'}]}
        self.s.configure('checkin',config)
        registry=AdapterRegistry(); registry.add('mail','selected',MarkdownAdapter(root))
        original=self.model
        def complete(**kw):
            result=original.complete_structured(**kw); note.write_text('Another unrelated email'); return result
        self.model=SimpleNamespace(complete_structured=complete)
        self.assertEqual(Checkins(self.s,self.model,registry,lambda:NOW,self.transport).tick(NOW),1)
        self.assertEqual(len(self.calls),1)

    def test_new_human_context_postpones_and_revalidates_without_erasing_draft(self):
        original=self.model
        def complete(**kw):
            result=original.complete_structured(**kw)
            with self.s.tx() as c:
                c.execute('INSERT INTO events VALUES(?,?)',('fresh',json.dumps({'id':'fresh','role':'human','principal':'fixture','text':'A separate conversation','observed_at':NOW})))
            return result
        self.model=SimpleNamespace(complete_structured=complete)
        clock=[NOW]
        self.assertEqual(self.engine(clock).tick(NOW),0)
        with self.s.tx() as c:
            day=dict(c.execute('SELECT * FROM checkin_days').fetchone())
        self.assertIsNotNone(day['draft'])
        self.assertEqual(day['reason'],'contextual_timing_deferred')
        self.model=original; clock[0]+=1801
        self.assertEqual(self.engine(clock).tick(clock[0]),1)
        self.assertEqual(len(self.calls),1)

    def test_cited_item_closed_during_synthesis_blocks_with_visible_reason(self):
        original=self.model
        def complete(**kw):
            result=original.complete_structured(**kw)
            self.s.transition(self.item['id'],0,'done')
            return result
        self.model=SimpleNamespace(complete_structured=complete)
        self.assertEqual(self.engine([NOW]).tick(NOW),0)
        with self.s.tx() as c:
            day=dict(c.execute('SELECT * FROM checkin_days').fetchone())
        self.assertEqual(day['state'],'blocked')
        self.assertEqual(day['reason'],'cited_ground_changed')
        self.assertFalse(self.sent)

    def test_cited_record_revision_change_invalidates_even_if_quote_still_present(self):
        original=self.model
        def complete(**kw):
            result=original.complete_structured(**kw)
            self.s.transition(self.item['id'],0,'snoozed',review=NOW+86400)
            return result
        self.model=SimpleNamespace(complete_structured=complete)
        self.assertEqual(self.engine([NOW]).tick(NOW),0)
        self.assertFalse(self.sent)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT reason FROM checkin_days').fetchone()[0],'cited_ground_changed')
