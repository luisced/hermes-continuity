"""Server-persisted messaging ingress, not model-declared producer labels."""
import hashlib
import json
import unittest
from tests import test_source_lineage as lineage
from continuity.source import NativeSource

class NativeMessagingTests(unittest.TestCase):
    setUp=lineage.SourceLineageTests.setup_source
    db=lineage.SourceLineageTests.db
    def admission(self, *, bot=False, connector='adapter:whatsapp:', owner='admission-one', principal=None):
        native={'event':{'message_id':'platform-one'},
                'source':{'platform':'whatsapp','chat_id':'chat-token','chat_type':'dm','thread_id':None,
                          'user_id':'live@pn','message_id':'platform-one','is_bot':bot},
                'provenance':{'platform':'whatsapp','connector':connector,'runtime_home':str(self.home),
                              'transport_home':str(self.home),'transport_profile':None},'route':'chat-route'}
        body={'text':self.payload['user_message'],'native_text_v1':native}
        encoded=json.dumps({'target':'reset-two','payload':{'input':body,'intent':'queue'}},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        principal=principal or 'messaging:'+json.dumps([None,'whatsapp','chat-token',None,'live@pn'],separators=(',',':'))
        with self.db() as c:
            c.executescript('''CREATE TABLE session_admissions(admission_id TEXT,request_id TEXT,principal_id TEXT,target_session_id TEXT,payload_json TEXT,payload_digest TEXT,intent TEXT,status TEXT);
                CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT,platform_message_id TEXT,display_metadata TEXT);''')
            c.execute('INSERT INTO session_admissions VALUES(?,?,?,?,?,?,?,?)',('admission-one','platform-one',principal,'reset-two',json.dumps(body),hashlib.sha256(encoded).hexdigest(),'queue','started'))
            c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('reset-two','user',body['text'],'platform-one',json.dumps({'gateway_input_owner':owner})))
    def test_native_human_admission_and_message_binding(self):
        self.admission()
        self.assertEqual(NativeSource(self.home).resolve(self.payload,self.identities),('whatsapp','stored@lid'))
    def test_unbound_message_is_not_human_authority(self):
        self.admission(owner='')
        with self.assertRaises(ValueError):NativeSource(self.home).resolve(self.payload,self.identities)
    def test_non_adapter_payload_cannot_claim_native_origin(self):
        self.admission(connector='plugin:forged')
        with self.assertRaises(ValueError):NativeSource(self.home).resolve(self.payload,self.identities)
    def test_bot_native_source_rejected(self):
        self.admission(bot=True)
        with self.assertRaises(ValueError):NativeSource(self.home).resolve(self.payload,self.identities)
    def test_principal_route_mismatch_rejected(self):
        self.admission(principal='messaging:'+json.dumps([None,'whatsapp','foreign-chat',None,'live@pn'],separators=(',',':')))
        with self.assertRaises(ValueError):NativeSource(self.home).resolve(self.payload,self.identities)
