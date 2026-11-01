"""Focused coverage for durable character sessions and their privacy boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from desktop_pet.agent.memory import MemoryStore
from desktop_pet.shell.recovery import load_last_reply, saved_reply, store_last_reply
from desktop_pet.shell.history import CharacterSession


class SessionHistoryTests(unittest.TestCase):
    def test_character_isolation_retention_and_context_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'shared.sqlite3'
            a = MemoryStore(db, 'nino')
            b = MemoryStore(db, 'kazusa')
            c = MemoryStore(db, 'chisato')
            for index in range(85):
                a.save_turn('same-conversation', str(index), f'user {index}',
                            {'speech_text_ja': f'reply {index}', 'provider_mode': 'codex'}, retain_turns=0)
            b.save_turn('same-conversation', 'b', 'private b', {'speech_text_ja': 'b reply'}, retain_turns=0)
            c.remember('private fact', key='one')
            a.remember('nino fact', key='one')
            b.remember('kazusa fact', key='one')
            self.assertEqual(len(a.conversation_history('same-conversation', limit=200)), 85)
            self.assertEqual(len(a.recent_messages('same-conversation')), 16)
            self.assertEqual(b.conversation_history('same-conversation')[0]['input_text'], 'private b')
            self.assertEqual(c.conversation_history('same-conversation'), [])
            self.assertEqual([item['body'] for item in a.list()], ['nino fact'])
            self.assertEqual([item['body'] for item in b.list()], ['kazusa fact'])
            self.assertEqual([item['body'] for item in c.list()], ['private fact'])
            bounded = MemoryStore(Path(temp) / 'bounded.sqlite3', 'bounded')
            for index in range(5):
                bounded.save_turn('c', str(index), str(index), {'speech_text_ja': 'r'}, retain_turns=2)
            self.assertEqual([r['turn_id'] for r in bounded.conversation_history('c')], ['3', '4'])

    def test_delivered_proactive_order_pagination_and_partial_heard(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / 'memory.sqlite3', 'nino')
            store.save_turn('c', 'manual', 'hello', {'speech_text_ja': 'full reply', 'provider_mode': 'x'}, retain_turns=0)
            store.record_delivery('c', 'manual', heard_text='', status='discarded')
            for turn, status, heard in [('cancel', 'cancelled', ''),
                                        ('full', 'delivered', 'heard fully'),
                                        ('part', 'interrupted', 'heard prefix')]:
                store.reserve_proactive_turn('c', turn, {'event_id': turn}, retain_turns=0)
                store.save_proactive_reply('c', turn, {'speech_text_ja': 'unheard generated'} )
                if status == 'cancelled':
                    store.discard_proactive_turn('c', turn)
                else:
                    store.record_delivery('c', turn, heard_text=heard, status=status)
            rows = store.conversation_history('c', limit=2)
            self.assertEqual([r['kind'] for r in rows], ['manual', 'proactive'])
            self.assertEqual(rows[0]['reply']['speech_text_ja'], 'full reply')
            all_rows = store.conversation_history('c', limit=10)
            self.assertEqual([r['heard_text'] for r in all_rows if r['kind'] == 'proactive'],
                             ['heard fully', 'heard prefix'])

    def test_helper_restores_formatted_markdown_and_fact_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = {'character_id': 'nino', 'display_name': '二乃',
                       'memory': {'database_name': 'memory.sqlite3'}}
            app = {'conversation_id': 'c'}
            session = CharacterSession(app, profile, root)
            text = '這張圖 attachment:' + 'a' * 64
            session.store.save_turn('c', 'turn', text,
                                    {'speech_text_ja': '返事', 'display_text_ja': '返事',
                                     'subtitle_zh': '字幕'}, retain_turns=0)
            parts = session.load_history_parts()
            self.assertEqual(parts[0], ('user', '你', text))
            self.assertEqual(parts[1], ('reply', '二乃', '返事', 'turn'))
            self.assertEqual(session.session_info()['history_count'], 1)
            fact = session.remember('user explicit fact')
            self.assertEqual(session.list_facts()[0]['body'], 'user explicit fact')
            self.assertTrue(session.delete_fact(fact['id']))
            self.assertEqual(session.list_facts(), [])

    def test_local_memory_turns_are_visible_and_paginate_without_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MemoryStore(Path(temp) / 'memory.sqlite3', 'nino')
            for index in range(25):
                store.save_turn('c', str(index), f'記住 fact {index}',
                                {'speech_text_ja': f'saved {index}', 'provider_mode': 'local_memory'},
                                retain_turns=0)
            first = store.conversation_history('c', limit=20)
            second = store.conversation_history('c', limit=20, offset=20)
            self.assertEqual(len(first), 20)
            self.assertEqual(len(second), 5)
            self.assertEqual([row['turn_id'] for row in first + second], [str(i) for i in range(25)])
            self.assertEqual(store.session_info('c')['history_count'], 25)

    def test_retry_cache_is_scoped_to_character_and_conversation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared_db = root / 'data' / 'memory.sqlite3'
            nino = MemoryStore(shared_db, 'nino')
            kazusa = MemoryStore(shared_db, 'kazusa')
            reply = {'turn_id': 'nino-turn', 'speech_text_ja': '再試行できます。',
                     'provider_mode': 'codex_subscription'}
            nino.save_turn('desktop-default', 'nino-turn', 'hello', reply, retain_turns=0)
            app_state = root / 'state'
            event_log = app_state / 'events.jsonl'
            event_log.parent.mkdir(parents=True)
            configs = {}
            for name in ('nino', 'kazusa'):
                profile_path = root / f'{name}.json'
                profile_path.write_text(json.dumps({'character_id': name,
                    'memory': {'database_name': 'memory.sqlite3'}}), encoding='utf-8')
                configs[name] = {'state_dir': str(app_state), 'event_log': str(event_log),
                                 'agent_config': str(profile_path), 'agent_data_dir': str(root / 'data'),
                                 'conversation_id': 'desktop-default'}
            store_last_reply(app_state / 'last-reply.json', reply, input_text='hello',
                             character_id='nino', conversation_id='desktop-default')
            event_log.write_text(json.dumps({'event': 'reply', 'reply': reply,
                'character_id': 'nino', 'conversation_id': 'desktop-default',
                'time': '2026-10-31T12:00:00+00:00'}) + '\n', encoding='utf-8')
            restored = load_last_reply(configs['nino'])
            self.assertEqual(restored['reply']['turn_id'], 'nino-turn')
            self.assertIsNone(load_last_reply(configs['kazusa']))

            # Old caches without identity metadata are accepted only when their
            # turn can be proven to belong to this profile in the SQLite journal.
            legacy = saved_reply(reply, input_text='hello')
            (app_state / 'last-reply.json').write_text(json.dumps(legacy), encoding='utf-8')
            self.assertEqual(load_last_reply(configs['nino'])['reply']['turn_id'], 'nino-turn')
            self.assertIsNone(load_last_reply(configs['kazusa']))


if __name__ == '__main__':
    unittest.main()
