import unittest
from unittest import mock
from backend.qa.actions import parse, Action
from backend.qa.browser import Session
from backend.qa.canvas_regions import parse_regions, CanvasRegions
from backend.qa.driver import Caps, Driver
from backend.qa.findings import RunLog
from backend.qa.personas import Persona
from backend.qa.prompts import RunBrief
from backend.test_support import make_temp_dir
from backend.tests.test_qa_driver import FakeSession, _raw_page


class CanvasFallbackTests(unittest.TestCase):
    def test_coordinate_grammar_and_region_bounds(self):
        for text in ('CLICK AT 200 150', 'CLICKAT 200 150'):
            action = parse(text)
            self.assertEqual((action.verb, action.x, action.y), ('CLICKAT', 200, 150))
            self.assertEqual(action.describe(), 'CLICK AT 200 150')
        regions = parse_regions('Menu\nREGION 200 150 "Start"\nREGION 2000 1 invalid\nREGION nan 1 invalid', (800, 600))
        self.assertEqual(regions.regions, [{'x': 200, 'y': 150, 'label': 'Start'}])
        observed = parse_regions('1. FAIRY DUEL: Centered at approximately 640, 340.\n2. Invisible: Centered at 9000, 340', (1280, 800))
        self.assertEqual(observed.regions, [{'x':640, 'y':340, 'label':'FAIRY DUEL'}])

    def test_inline_vision_clauses_preserve_each_reported_control(self):
        answer = ('Fairy Duel setup. Visible controls are: '
                  'REGION 540 350 Gentle, REGION 620 350 Standard; '
                  'REGION 700 540 Start Duel, REGION 9000 350 Outside')
        observed = parse_regions(answer, (1280, 800))
        self.assertEqual(observed.regions, [
            {'x': 540, 'y': 350, 'label': 'Gentle'},
            {'x': 620, 'y': 350, 'label': 'Standard'},
            {'x': 700, 'y': 540, 'label': 'Start Duel'},
        ])
        self.assertIn('Fairy Duel setup', observed.description)

    def test_real_canvas_click_and_outside_coordinate_rejection(self):
        session = Session(viewport=(800, 600)).start()
        self.addCleanup(session.close)
        session.page.set_content('''<style>body{margin:0}</style><canvas width="800" height="600"></canvas>
        <script>const c=document.querySelector('canvas');const ctx=c.getContext('2d');ctx.fillRect(100,100,200,100);
        c.addEventListener('click',e=>{if(e.clientX>=100&&e.clientX<=300&&e.clientY>=100&&e.clientY<=200)document.title='Board reached'});</script>''')
        snapshot = session.snapshot()
        self.assertTrue(snapshot.is_canvas_only)
        invalid = session.execute(Action('CLICKAT', x=900, y=150), snapshot)
        self.assertIn('outside', invalid.outcome)
        self.assertNotEqual(session.page.title(), 'Board reached')
        session.execute(parse('CLICK AT 200 150'), snapshot)
        self.assertEqual(session.page.title(), 'Board reached')

    def test_driver_receives_regions_and_uses_visual_state_for_loop_detection(self):
        page = _raw_page(); page['rows'] = []; page['canvases'] = 1
        session = FakeSession(pages=[page])
        session.screenshot_b64 = lambda: f'screen-{session._index}'
        seen = []
        chat = lambda system, user: seen.append(user) or 'CLICK AT 200 150'
        reader = mock.Mock(return_value=CanvasRegions('Menu', [{'x': 200, 'y': 150, 'label': 'Start'}], 'vision'))
        log = RunLog(make_temp_dir(self, prefix='canvas-driver-'))
        driver = Driver(session, log, Persona('test', 'test', 'test'), RunBrief('Reach board'),
                        caps=Caps(steps=3, minutes=1, vision_every=5), model='stub', chat=chat, canvas_reader=reader)
        outcome = driver.run('http://localhost/test')
        self.assertEqual(outcome.reason, 'steps')
        self.assertEqual(len(session.executed), 3)
        self.assertIn('CLICK AT 200 150', seen[0])
        self.assertEqual(reader.call_count, 3)

    def test_keyboard_persona_cannot_bypass_mouse_forbid_with_coordinates(self):
        page = _raw_page(); page['rows'] = []; page['canvases'] = 1
        session = FakeSession(pages=[page])
        log = RunLog(make_temp_dir(self, prefix='canvas-keyboard-'))
        Driver(session, log, Persona('test', 'test', 'test', forbid=('CLICK',)), RunBrief('Reach board'),
               caps=Caps(steps=1, minutes=1, vision_every=0), model='stub', chat=lambda *_: 'CLICK AT 200 150').run('http://localhost/test')
        self.assertEqual(session.executed, [])
