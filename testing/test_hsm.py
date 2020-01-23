# (c) Copyright 2020 by Coinkite Inc. This file is part of Coldcard <coldcardwallet.com>
# and is covered by GPLv3 license found in COPYING.
#
# Test HSM and its policy file.
#
# For testing on a REAL Coldcard Mk3:
# - enable dev mode on Coldcard, and copy ../unix/frozen-modules/usb_test_commands.py
#   to /lib on Coldcard internal FS .. might need custom firmware/bootrom
# - set coldcard for testnet chain
# - command line: py.test test_hsm.py --dev -s --ff
# - no microSD card installed
#
import pytest, time, struct, os, itertools
#from pycoin.key.BIP32Node import BIP32Node
from binascii import b2a_hex, a2b_hex
from hashlib import sha256
from ckcc_protocol.protocol import MAX_MSG_LEN, CCProtocolPacker, CCProtoError
from ckcc_protocol.protocol import CCUserRefused, CCProtoError
from ckcc_protocol.protocol import USER_AUTH_TOTP, USER_AUTH_HOTP, USER_AUTH_HMAC

import json
from pprint import pprint
from objstruct import ObjectStruct as DICT
from txn import *
from ckcc_protocol.constants import *

TEST_USERS = { 
            # time based OTP
            # otpauth://totp/totp?secret=UR4LAZMTSJOF52FE&issuer=Coldcard%20simulator
            'totp': [1, 'UR4LAZMTSJOF52FE', 0],

            # OBSCURE: counter-based, not time
            # - no way to get your counter in sync w/ simulator
            # otpauth://hotp/hotp?secret=DBDCOKLQKM6BAKXD&issuer=Coldcard%20simulator
            'hotp': [2, 'DBDCOKLQKM6BAKXD', 0],

            # password
            # pw / 1234abcd
            'pw': [3, 'THNUHHFTG44NLI4EC7H7D6MU5AYMC3B3ER2ZFIBHQVUBOLGADA7Q', 0],
        }
USERS = list(TEST_USERS.keys())

# example dest addrs
EXAMPLE_ADDRS = [ '1ByzQTr5TCkMW9RH1fkD7QtnMbErffDeUo', '2N4EDPkGYcZa5o6kFou2g9zEyiTjk27Jt5D',
            '3Cg1L1LX174jbK7i8mQoY3FiW7XaDs9oRX', 'mrVwhWw4GEBcHFttjEiawL77DaqZWNDm75',
            'tb1q0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rclglv65',
            'bc1q0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc4wylp8',
            'bc1q0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0puqxn6udr',
            'tb1q0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0pu8s7rc0puq3mvnhv',
]


@pytest.fixture(scope='function')
def hsm_reset(dev, sim_exec):
    # filename for the policy file, as stored on simulated CC

    def doit():
        # make sure we can setup an HSM now; often need to restart simulator tho

        # clear defined config
        cmd = 'import uos, hsm; uos.unlink(hsm.POLICY_FNAME)'
        sim_exec(cmd)

        # reset HSM code, to clear previous HSM setup
        while 1:
            j = json.loads(dev.send_recv(CCProtocolPacker.hsm_status()))
            if j.get('active') == False:
                break

            # reset out of HSM mode
            cmd = 'from hsm_ux import hsm_ux_obj; hsm_ux_obj.test_restart = True'
            sim_exec(cmd)
            time.sleep(.1)

    yield doit

    try:
        cmd = 'import uos, hsm; uos.unlink(hsm.POLICY_FNAME)'
        sim_exec(cmd)
    except:
        pass

@pytest.mark.parametrize('policy,contains', [
    (DICT(), 'No transaction will be signed'),
    (DICT(must_log=1), 'MicroSD card MUST '),
    (DICT(must_log=0), 'MicroSD card will '),
    (DICT(never_log=1), 'No logging'),
    (DICT(warnings_ok=1), 'PSBT warnings'),

    (DICT(msg_paths=["m/1'/2p/3H"]), "m/1'/2'/3'"),
    (DICT(msg_paths=["m/1", "m/2"]), "m/1 OR m/2"),
    (DICT(msg_paths=["any"]), "(any path)"),

    (DICT(share_addrs=["m/1'/2p/3H"]), ['Address values values will be shared', "m/1'/2'/3'"]),
    (DICT(share_addrs=["m/1", "m/2"]), ['Address values values will be shared', "m/1 OR m/2"]),
    (DICT(share_addrs=["any"]), ['Address values values will be shared', "(any path)"]),
    (DICT(share_addrs=["p2sh", "any"]), ['Address values values will be shared', "(any P2SH)", "(any path"]),

    (DICT(share_xpubs=["m/1'/2p/3H"]), ['XPUB values will be shared', "m/1'/2'/3'"]),
    (DICT(share_xpubs=["m/1", "m/2"]), ['XPUB values will be shared', "m/1 OR m/2"]),
    (DICT(share_xpubs=["any"]), ['XPUB values will be shared', "(any path)"]),

    (DICT(notes='sdfjkljsdfljklsdf'), 'sdfjkljsdfljklsdf'),

    (DICT(period=2), '2 minutes'),
    (DICT(period=60), '1 hrs'),
    (DICT(period=5*60), '5 hrs'),
    (DICT(period=3*24*60), '72 hrs'),

    (DICT(allow_sl=1), 'once'),
    (DICT(allow_sl=10), '10 times'),
    (DICT(set_sl='abcd'*4, allow_sl=1), 'Locker will be updated'),

    # period / max amount
    (DICT(period=60, rules=[dict(per_period=1000)]),
        '0.00001000 XTN per period'),
    (DICT(period=60, rules=[dict(per_period=1000, max_amount=2000)]),
        'and up to 0.00002000 XTN per txn'),
    (DICT(period=60, rules=[dict(max_amount=3000)]),
        'Up to 0.00003000 XTN per txn'),
    (DICT(rules=[dict(max_amount=3000)]),
        'Up to 0.00003000 XTN per txn'),
    (DICT(rules=[dict()]),
        'Any amount will be approved'),

    # wallets
    (DICT(rules=[dict(wallet='1')]),
        '(non multisig)'),

    # users
    (DICT(rules=[dict(users=USERS)]),
        'Any amount may be authorized by all users'),
    (DICT(rules=[dict(min_users=1, users=USERS)]),
        'Any amount may be authorized by any one user'),
    (DICT(rules=[dict(min_users=2, users=USERS)]),
        'Any amount may be authorized by at least 2 users'),

    # whitelist
    (DICT(rules=[dict(whitelist=['131CnJGaDyPaJsb5P4NHFxcRi29zo3ZXw'])]),
        'provided it goes to: 131CnJGaDyPaJsb5P4NHFxcRi29zo3ZXw'),
    (DICT(rules=[dict(whitelist=EXAMPLE_ADDRS)]),
        'provided it goes to: '+ ', '.join(EXAMPLE_ADDRS)),

    # if local user confirms
    (DICT(rules=[dict(local_conf=True)]),
        'if local user confirms'),

    # multiple rules
    (DICT(rules=[dict(local_conf=True), dict(max_amount=1E8)]),
        'Rule #2'),
])
def test_policy_parsing(sim_exec, policy, contains, load_hsm_users):
    # Unit test on parsing!

    load_hsm_users()

    cmd = f"from hsm import HSMPolicy; a=HSMPolicy(); a.load({dict(policy)}); a.explain(RV)"

    got = sim_exec(cmd)
    print(got)

    assert 'Other policy' in got
    assert 'Transactions:\n' in got
    assert 'Message signing:\n' in got
    assert 'Other policy:\n' in got

    if 'rules' not in policy:
        assert 'No transaction will be signed' in got
    else:
        for n in range(len(policy['rules'])):
            assert 'Rule #%d'%(n+1) in got

    if getattr(policy, 'msg_paths', None):
        assert '- Allowed if path is: '

    if getattr(policy, 'period', None):
        assert '%d minutes\n'%policy.period in got

    if isinstance(contains, str):
        assert contains in got
    else:
        assert all(c in got for c in contains)


@pytest.fixture
def tweak_rule(sim_exec):
    # reach under the skirt, and change policy rule ... so much faster

    def doit(idx, new_rule):
        #cmd = f"from hsm import ApprovalRule; from main import hsm_active; hsm_active.rules[{idx}] = ApprovalRule({dict(new_rule)}, {idx}); hsm_active.summary='**tweaked**'; RV.write(hsm_active.rules[{idx}].to_text())"
        #print(f"Rule #{idx+1} now: {txt}")
        cmd = f"from hsm import ApprovalRule; from main import hsm_active; hsm_active.rules[{idx}] = ApprovalRule({dict(new_rule)}, {idx}); hsm_active.summary='**tweaked**'; RV.write('ok')"
        txt = sim_exec(cmd)
        if 'Traceback' in txt:
            raise RuntimeError(txt)
        assert txt == 'ok'

    return doit

@pytest.fixture
def readback_rule(sim_exec):
    # readback the stored config of a rule, after parsing
    def doit(idx):
        cmd = f"import ujson; from main import hsm_active; RV.write(ujson.dumps(hsm_active.rules[{idx}].to_json()));"
        txt = sim_exec(cmd)
        if 'Traceback' in txt:
            raise RuntimeError(txt)
        return json.loads(txt, object_hook=DICT)
    return doit

@pytest.fixture
def tweak_hsm_attr(sim_exec):
    # reach under the skirt, and change and attr on hsm obj
    def doit(name, value):
        cmd = f"from main import hsm_active; setattr(hsm_active, '{name}', {value})"
        sim_exec(cmd)
    return doit

@pytest.fixture
def tweak_hsm_method(sim_exec):
    # reach under the skirt, and change and attr on hsm obj
    def doit(fcn_name, *args):
        cmd = f"from main import hsm_active; getattr(hsm_active, '{name}')({', '.join(args)})"
        sim_exec(cmd)
    return doit


@pytest.fixture
def load_hsm_users(dev, settings_set):
    def doit(u=None):
        from base64 import b32encode
        TEST_USERS['pw'][1] = b32encode(calc_hmac_key(dev.serial)).decode('ascii').rstrip('=')

        settings_set('usr', u or TEST_USERS)
    return doit

@pytest.fixture
def hsm_status(dev):

    def doit(timeout=1000):
        txt = dev.send_recv(CCProtocolPacker.hsm_status(), timeout=timeout)
        assert txt[0] == '{'
        assert txt[-1] == '}'
        j = json.loads(txt, object_hook=DICT)
        assert j.active in {True, False}
        return j

    return doit

@pytest.fixture
def change_hsm(sim_eval, sim_exec, hsm_status):
    # change policy after HSM is running.
    def doit(policy):
        # if already an HSM in motion; just replace it quickly

        act = sim_eval('main.hsm_active')
        assert act != 'None', 'hsm not enabled yet'

        cmd = f"import main; from hsm import HSMPolicy; \
                    p=HSMPolicy(); p.load({dict(policy)}); main.hsm_active=p; p.explain(RV)"
        rv = sim_exec(cmd)
        assert 'Other policy' in rv

        return hsm_status()
    return doit

@pytest.fixture
def quick_start_hsm(hsm_reset, start_hsm, hsm_status, change_hsm, sim_eval):
    # if already an HSM in motion; just replace it quickly
    def doit(policy):
        act = sim_eval('main.hsm_active')

        if act != 'None':
            return change_hsm(policy)
        else:
            return start_hsm(policy)
    return doit

@pytest.fixture
def start_hsm(request, dev, hsm_reset, hsm_status, need_keypress):
    
    def doit(policy):
        try:
            # on simulator, can read screen and provide keystrokes
            cap_story = request.getfixturevalue('cap_story')
        except:
            # real hardware
            cap_story = None

        # send policy, start it, approve it
        data = json.dumps(policy).encode('ascii')

        ll, sha = dev.upload_file(data)
        assert ll == len(data)

        dev.send_recv(CCProtocolPacker.hsm_start(ll, sha))

        if cap_story:
            # capture explanation given user
            time.sleep(.2)
            title, body = cap_story()
            assert title == "Start HSM?"

        if cap_story:
            # approve it
            need_keypress('y')
            time.sleep(.1)

            title, body2 = cap_story()
            assert 'Last chance' in body2
            ll = body2.split('\n')[-1]
            assert ll.startswith("Press ")
            ch = ll[6]

            need_keypress(ch)
            time.sleep(.100)

            j = hsm_status()
            assert j.active == True
            assert not body or j.summary in body

        else:
            # do keypresses blindly
            need_keypress('y')
            time.sleep(.1)
            for ch in '12346':
                need_keypress(ch, timeout=10000)

            # needs to bless firmware step; can take >10 seconds?
            j = hsm_status(10000)
            assert j.active == True

            if 0:
                for retry in range(30):
                    time.sleep(1)
                    #try: except: pass
                assert j.active == True

        return j

    # setup: remove any existing HSM setup
    hsm_reset()

    # fixture ready
    yield doit

def wait_til_signed(dev):
    result = None
    while result == None:
        time.sleep(0.050)
        result = dev.send_recv(CCProtocolPacker.get_signed_txn(), timeout=None)

    return result

@pytest.fixture
def attempt_psbt(hsm_status, start_sign, dev):

    def doit(psbt, refuse=None, remote_error=None):
        open('debug/attempt.psbt', 'wb').write(psbt)
        start_sign(psbt)

        try:
            resp_len, chk = wait_til_signed(dev)
            assert refuse == None, "should have been refused: " + refuse
        except CCProtoError as exc:
            assert remote_error, "unexpected remote error: %s" % exc
            if remote_error not in str(exc):
                raise
        except CCUserRefused:
            msg = hsm_status().last_refusal
            assert refuse != None, "should not have been refused: " + msg
            #assert msg.startswith('Rejected: ')
            assert refuse in msg

            return msg

    return doit

@pytest.fixture
def attempt_msg_sign(dev, hsm_status):
    def doit(refuse, *args, **kws):
        tt = kws.pop('timeout', None)
        dev.send_recv(CCProtocolPacker.sign_message(*args, **kws), timeout=tt)

        try:
            done = None
            while done == None:
                time.sleep(0.050)
                done = dev.send_recv(CCProtocolPacker.get_signed_msg(), timeout=tt)

            assert len(done) == 2
            assert refuse == None, "signing didn't fail, but expected to"
        except CCUserRefused:
            msg = hsm_status().last_refusal
            assert refuse != None, "should not have been refused: " + msg
            assert refuse in msg

    return doit

@pytest.mark.parametrize('amount', [ 1E4, 1E6, 1E8 ])
@pytest.mark.parametrize('over', [ 1, 1000])
def test_simple_limit(dev, amount, over, start_hsm, fake_txn, attempt_psbt, tweak_rule):
    # a policy which sets a hard limit
    policy = DICT(rules=[dict(max_amount=amount)])

    stat = start_hsm(policy)
    assert ('Up to %.8f XTN per txn will be approved' % (amount/1E8)) in stat.summary
    assert 'Rule #1' in stat.summary
    assert 'Rule #2' not in stat.summary

    # create a transaction
    psbt = fake_txn(2, 2, dev.master_xpub, outvals=[amount, 2E8-amount],
                        change_outputs=[1], fee=0)
    attempt_psbt(psbt)

    psbt = fake_txn(2, 2, dev.master_xpub, outvals=[amount+over, 2E8-amount-over],
                                                    change_outputs=[1], fee=0)
    attempt_psbt(psbt, "amount exceeded")

    if tweak_rule:
        tweak_rule(0, dict(max_amount=int(amount+over)))
        attempt_psbt(psbt)

def test_named_wallets(dev, start_hsm, tweak_rule, make_myself_wallet, hsm_status, attempt_psbt, fake_txn, fake_ms_txn, amount=5E6, incl_xpubs=False):
    wname = 'Myself-4'
    M = 4

    stat = hsm_status()
    assert not stat.active

    for retry in range(3):
        keys, _ = make_myself_wallet(4)       # slow AF

        stat = hsm_status()
        if wname in stat.wallets:
            break

    # policy: only allow multisig w/ that name
    policy = DICT(rules=[dict(wallet=wname)])

    stat = start_hsm(policy)
    assert 'Any amount from multisig wallet' in stat.summary
    assert wname in stat.summary
    assert 'wallets' not in stat

    # simple p2pkh should fail

    psbt = fake_txn(1, 2, dev.master_xpub, outvals=[amount, 1E8-amount], change_outputs=[1], fee=0)
    attempt_psbt(psbt, "not multisig")

    # but txn w/ multisig wallet should work
    psbt = fake_ms_txn(1, 2, M, keys, fee=0, outvals=[amount, 1E8-amount], outstyles=['p2wsh'],
                                    change_outputs=[1], incl_xpubs=incl_xpubs)
    attempt_psbt(psbt)

    # check ms txn not accepted when rule spec's a single signer
    tweak_rule(0, dict(wallet='1'))
    attempt_psbt(psbt, 'wrong wallet')


def test_whitelist_single(dev, start_hsm, tweak_rule, attempt_psbt, fake_txn, amount=5E6):
    junk = EXAMPLE_ADDRS[0]
    policy = DICT(rules=[dict(whitelist=[junk])])
    started = False

    start_hsm(policy)

    # try all addr types
    for style in ['p2wpkh', 'p2wsh', 'p2sh', 'p2pkh', 'p2wsh-p2sh', 'p2wpkh-p2sh']:
        dests = []
        psbt = fake_txn(1, 2, dev.master_xpub,
                            outstyles=[style, 'p2wpkh'],
                            outvals=[amount, 1E8-amount], change_outputs=[1], fee=0,
                            capture_scripts=dests)

        dest = render_address(dests[0])

        tweak_rule(0, dict(whitelist=[dest]))
        attempt_psbt(psbt)

        tweak_rule(0, dict(whitelist=[junk]))
        attempt_psbt(psbt, "non-whitelisted")

        tweak_rule(0, dict(whitelist=[dest, junk]))
        attempt_psbt(psbt)

def test_whitelist_multi(dev, start_hsm, tweak_rule, attempt_psbt, fake_txn, amount=5E6):
    # sending to one whitelisted, and one non, etc.
    junk = EXAMPLE_ADDRS[0]
    policy = DICT(rules=[dict(whitelist=[junk])])

    stat = start_hsm(policy)

    # make a txn that sends to every type of output
    styles = ['p2wpkh', 'p2wsh', 'p2sh', 'p2pkh', 'p2wsh-p2sh', 'p2wpkh-p2sh']
    dests = []
    psbt = fake_txn(1, len(styles), dev.master_xpub,
                        outstyles=styles, capture_scripts=dests)

    dests = [render_address(s) for s in dests]

    # simple: sending to all
    tweak_rule(0, dict(whitelist=dests))
    attempt_psbt(psbt)

    # whitelist only one of those (expect fail)
    for dest in dests:
        tweak_rule(0, dict(whitelist=[dest]))
        msg = attempt_psbt(psbt, 'non-whitelisted')
        nwl = msg.rsplit(': ', 1)[1]
        # random addr is put in err msg
        assert nwl != dest
        assert nwl in dests

    # whitelist all but one of them
    for dest in dests:
        others = [d for d in dests if d != dest]
        tweak_rule(0, dict(whitelist=others))
        msg = attempt_psbt(psbt, 'non-whitelisted')
        # sing addr is put in err msg
        nwl = msg.rsplit(': ', 1)[1]
        assert nwl == dest
        assert nwl in dests

@pytest.mark.parametrize('warnings_ok', [ False, True])
def test_huge_fee(warnings_ok, dev, quick_start_hsm, hsm_status, tweak_hsm_attr, attempt_psbt, fake_txn, amount=5E6):
    # fee over 50% never good idea
    # - doesn't matter what current policy is
    policy = {'warnings_ok': warnings_ok, 'rules': [{}]}

    stat = quick_start_hsm(policy)

    tweak_hsm_attr('warnings_ok', warnings_ok)

    psbt = fake_txn(1, 1, dev.master_xpub, fee=0.5E8)
    attempt_psbt(psbt, remote_error='Network fee bigger than 10% of total')

    psbt = fake_txn(1, 1, dev.master_xpub, fee=100)
    attempt_psbt(psbt)

def test_psbt_warnings(dev, quick_start_hsm, tweak_hsm_attr, attempt_psbt, fake_txn, amount=5E6):
    # txn w/ warnings
    policy = DICT(warnings_ok=True, rules=[{}])
    stat = quick_start_hsm(policy)
    assert 'warnings' in stat.summary

    psbt = fake_txn(1, 1, dev.master_xpub, fee=0.05E8)
    attempt_psbt(psbt)

    tweak_hsm_attr('warnings_ok', False)
    attempt_psbt(psbt, 'has 1 warning(s)')

@pytest.mark.parametrize('num_out', [11, 50])
@pytest.mark.parametrize('num_in', [10, 20])
def test_big_txn(num_in, num_out, dev, quick_start_hsm, hsm_status, is_simulator,
                            tweak_hsm_attr, attempt_psbt, fake_txn, amount=5E6):

    if not is_simulator():
        # It does work, I've done it, but let's never do it again...
        raise pytest.skip("life is too short")

    # do something slow
    policy = DICT(warnings_ok=True, rules=[{}])
    quick_start_hsm(policy)

    for count in range(20):
        psbt = fake_txn(num_in, num_out, dev.master_xpub)
        attempt_psbt(psbt)


def test_sign_msg_good(quick_start_hsm, change_hsm, attempt_msg_sign, addr_fmt=AF_CLASSIC):
    # message signing, but only at certain derivations
    permit = ['m/73', 'm/1p/3h/4/5/6/7' ]
    block = ['m', 'm/72', permit[-1][:-2]]
    msg = b'testing 123'

    policy = DICT(msg_paths=permit)
    quick_start_hsm(policy)

    if 1:
        for addr_fmt in  [ AF_CLASSIC, AF_P2WPKH, AF_P2WPKH_P2SH]:

            for p in permit: 
                attempt_msg_sign(None, msg, p, addr_fmt=addr_fmt)

            for p in block:
                attempt_msg_sign('not enabled for that path', msg, p, addr_fmt=addr_fmt)

    policy = DICT(msg_paths=['any'])
    change_hsm(policy)

    for p in block+permit: 
        attempt_msg_sign(None, msg, p, addr_fmt=addr_fmt)

def test_sign_msg_any(quick_start_hsm, attempt_msg_sign, addr_fmt=AF_CLASSIC):
    permit = ['m/73', 'm/1p/3h/4/5/6/7' ]
    block = ['m', 'm/72', permit[-1][:-2]]
    msg = b'whatever'

    policy = DICT(msg_paths=['any'])
    quick_start_hsm(policy)

    for p in permit+block: 
        attempt_msg_sign(None, msg, p, addr_fmt=addr_fmt)

def test_must_log(dev, start_hsm, sim_card_ejected, attempt_msg_sign, fake_txn, attempt_psbt, is_simulator):
    # stop everything if can't log
    policy = DICT(must_log=True, msg_paths=['m'], rules=[{}])

    start_hsm(policy)

    psbt = fake_txn(1, 1, dev.master_xpub)

    sim_card_ejected(True)
    attempt_msg_sign('Could not log details', b'hello', 'm', addr_fmt=AF_CLASSIC)
    attempt_psbt(psbt, 'Could not log details')

    if is_simulator():
        sim_card_ejected(False)
        attempt_msg_sign(None, b'hello', 'm', addr_fmt=AF_CLASSIC)
        attempt_psbt(psbt)

def test_never_log(dev, start_hsm, attempt_msg_sign, fake_txn, attempt_psbt, sim_card_ejected):
    # never try to log anything
    policy = DICT(never_log=True, msg_paths=['m'], rules=[{}])

    start_hsm(policy)

    sim_card_ejected(True)

    # WEAK test
    attempt_msg_sign(None, b'hello', 'm', addr_fmt=AF_CLASSIC)

@pytest.fixture
def enter_local_code(need_keypress):
    def doit(code):
        assert len(code) == 6 and code.isdigit()
        need_keypress('x', timeout=5000)
        for ch in code:
            need_keypress(ch, timeout=5000)
        need_keypress('y', timeout=5000)

        # need this because UX loop for HSM has long sleep in it
        time.sleep(.250)

    return doit

# dev serial number is part of salt, stored PW value, and challenge
# both need to follow that.
def calc_hmac_key(serial, secret='abcd1234'):
    from hashlib import pbkdf2_hmac, sha256
    from ckcc_protocol.constants import PBKDF2_ITER_COUNT

    salt = sha256(b'pepper'+serial.encode('ascii')).digest()
    key = pbkdf2_hmac('sha256', secret.encode('ascii'), salt, PBKDF2_ITER_COUNT)

    return key

@pytest.fixture
def auth_user(dev):

    from onetimepass import get_hotp
    
    class State:
        def __init__(self):
            # start time only; don't want to wait 30 seconds between steps
            self.tt = int(time.time() // 30)
            # counter for HOTP
            self.ht = 3
            self.psbt_hash = None

        def __call__(self, username, garbage=False, do_replay=False):
            # calc right values!
            from base64 import b32decode

            mode, secret, _ = TEST_USERS[username]

            if garbage:
                pw = b'\x12'*32 if mode == USER_AUTH_HMAC else b'123x23'
                cnt = (self.tt if mode == USER_AUTH_TOTP else 0)
            elif mode == USER_AUTH_HMAC:
                assert len(self.psbt_hash) == 32
                assert username == 'pw'
                cnt = 0

                from hmac import HMAC

                key = calc_hmac_key(dev.serial) 
                pw = HMAC(key, self.psbt_hash, sha256).digest()

                #print("\n  pw=%s\n key=%s\npsbt=%s\nsalt=%s\n" % (
                #    b2a_hex(pw),
                #    b2a_hex(key),
                #    b2a_hex(self.psbt_hash),
                #    b2a_hex(salt)))

                assert not do_replay
            else:
                if do_replay:
                    if mode == USER_AUTH_TOTP:
                        cnt = self.tt-1
                    elif mode == USER_AUTH_HOTP:
                        cnt = self.ht-1
                else:
                    if mode == USER_AUTH_TOTP:
                        cnt = self.tt; self.tt += 1
                    elif mode == USER_AUTH_HOTP:
                        cnt = self.ht; self.ht += 1

                pw = b'%06d' % get_hotp(secret, cnt)

            assert len(pw) in {6, 32}

            # no feedback from device at this point.
            dev.send_recv(CCProtocolPacker. user_auth(username.encode('ascii'), pw, totp_time=cnt))

    return State()


def test_invalid_psbt(quick_start_hsm, attempt_psbt):
    policy = DICT(warnings_ok=True, rules=[{}])
    quick_start_hsm(policy)
    garb = b'psbt\xff'*20
    attempt_psbt(garb, remote_error='PSBT parse failed')

    # even w/o any signing rights, invalid is invalid
    policy = DICT()
    quick_start_hsm(policy)
    attempt_psbt(garb, remote_error='PSBT parse failed')

@pytest.mark.parametrize('package', [
    "hello world; how's tricks?",
    'OGlICrIPZE6DEtsGfcWH2pO6Uz6ZI+w05BYOERMN0XahGicvBhSR4HcgcX3mzk/qM3dWFZ8QAOEIvPFujlhULg==',
    ])
@pytest.mark.parametrize('count', [1, 5])
def test_storage_locker(package, count, start_hsm, dev):
    # read and write (limited) of storage locker.

    policy = DICT(set_sl=package, allow_sl=count)
    start_hsm(policy)


    for t in range(count+3):
        if t < count:
            got = dev.send_recv(CCProtocolPacker.get_storage_locker(), timeout=None)
            assert got == package.encode('ascii')
        else:
            with pytest.raises(CCProtoError) as ee:
                got = dev.send_recv(CCProtocolPacker.get_storage_locker(), timeout=None)
            assert 'consumed' in str(ee)

def test_usb_cmds_block(quick_start_hsm, dev):
    # check these commands return errors (test whitelist)
    block_list = [
        'rebo', 'dfu_', 'enrl', 'enok',
        'back', 'pass', 'bagi', 'hsms', 'nwur', 'rmur', 'pwok', 'bkok',
    ]

    quick_start_hsm(dict())

    for cmd in block_list:
        with pytest.raises(CCProtoError) as ee:
            got = dev.send_recv(cmd)
        assert 'HSM' in str(ee)

def test_unit_local_conf(sim_exec, enter_local_code, quick_start_hsm):
    # just testing our fixture really
    quick_start_hsm({})

    enter_local_code('123456')
    rb = sim_exec('from main import hsm_active; RV.write(hsm_active.local_code_pending)')
    assert rb == '123456'


def test_show_addr(dev, quick_start_hsm, change_hsm):
    # test we can do address "showing" with no UX
    # which can also be disabled, etc.
    path = 'm/4'
    addr_fmt = AF_P2WPKH
    policy = DICT(share_addrs=[path])
    
    def doit(path, addr_fmt):
        return dev.send_recv(CCProtocolPacker.show_address(path, addr_fmt), timeout=5000)

    quick_start_hsm(policy)
    addr = doit(path, addr_fmt)

    change_hsm(DICT(share_addrs=['m']))
    with pytest.raises(CCProtoError) as ee:
        addr = doit(path, addr_fmt)
    assert 'Not allowed in HSM mode' in str(ee)

    addr = doit('m', addr_fmt)

    change_hsm(DICT(share_addrs=['any']))
    addr = doit('m', addr_fmt)
    addr = doit('m/1/2/3', addr_fmt)
    addr = doit('m/3', addr_fmt)

    permit = ['m/73', 'm/1p/3h/4/5/6/7', 'm/1/2/3' ]
    change_hsm(DICT(share_addrs=permit))
    for path in permit:
        addr = doit(path, addr_fmt)

def test_show_p2sh_addr(dev, hsm_reset, start_hsm, change_hsm, make_myself_wallet, addr_vs_path):
    # MULTISIG addrs
    from test_multisig import HARD, make_redeem
    M = 4
    pm = lambda i: [HARD(45), i, 0,0]

    # can't amke ms wallets inside HSM mode
    hsm_reset()
    keys, _ = make_myself_wallet(M)       # slow AF

    permit = ['p2sh', 'm/73']
    start_hsm(DICT(share_addrs=permit))


    scr, pubkeys, xfp_paths = make_redeem(M, keys, path_mapper=pm)
    assert len(scr) <= 520, "script too long for standard!"

    got_addr = dev.send_recv(CCProtocolPacker.show_p2sh_address(
                                    M, xfp_paths, scr, addr_fmt=AF_P2WSH))
    addr_vs_path(got_addr, addr_fmt=AF_P2WSH, script=scr)

    # turn it off; p2sh must be explicitly allowed
    for allow in ['m', 'any']:
        change_hsm(DICT(share_addrs=[allow]))
        dev.send_recv(CCProtocolPacker.show_address('m', AF_CLASSIC))

        with pytest.raises(CCProtoError) as ee:
            got_addr = dev.send_recv(CCProtocolPacker.show_p2sh_address(
                                    M, xfp_paths, scr, addr_fmt=AF_P2WSH))
        assert 'Not allowed in HSM mode' in str(ee)

def test_xpub_sharing(dev, start_hsm, change_hsm, addr_fmt=AF_CLASSIC):
    # message signing, but only at certain derivations
    permit = ['m/73', 'm/1p/3h/4/5/6/7' ]
    block = ['m', 'm/72', permit[-1][:-2]]

    policy = DICT(share_xpubs=permit)
    start_hsm(policy)

    for p in permit: 
        xpub = dev.send_recv(CCProtocolPacker.get_xpub(p), timeout=5000)

        for p in block:
            with pytest.raises(CCProtoError) as ee:
                xpub = dev.send_recv(CCProtocolPacker.get_xpub(p), timeout=5000)
            assert 'Not allowed in HSM mode' in str(ee)

    policy = DICT(share_xpubs=['any'])
    change_hsm(policy)

    for p in block+permit: 
        xpub = dev.send_recv(CCProtocolPacker.get_xpub(p), timeout=5000)

@pytest.fixture
def fast_forward(sim_exec):
    def doit(dt):
        cmd = f'from main import hsm_active; hsm_active.period_started -= {dt}; RV.write("ok")'
        assert sim_exec(cmd) == 'ok'
    return doit

def test_velocity(dev, start_hsm, fake_txn, attempt_psbt, fast_forward, hsm_status):
    # stop everything if can't log
    level = int(1E8)
    policy = DICT(period=2, rules=[dict(per_period=level)])

    start_hsm(policy)

    psbt = fake_txn(2, 1, dev.master_xpub)
    attempt_psbt(psbt, 'would exceed period spending')

    psbt = fake_txn(2, 2, dev.master_xpub)
    attempt_psbt(psbt, 'would exceed period spending')

    psbt = fake_txn(2, 10, dev.master_xpub)
    attempt_psbt(psbt, 'would exceed period spending')

    psbt = fake_txn(2, 2, dev.master_xpub, outvals=[level, 2E8-level], change_outputs=[1])
    attempt_psbt(psbt)      # exactly the limit

    s = hsm_status()
    assert 90 <= s.period_ends <= 120
    assert s.has_spent == [level]

    attempt_psbt(psbt, 'would exceed period spending')

    psbt = fake_txn(1, 1, dev.master_xpub)
    attempt_psbt(psbt, 'would exceed period spending')

    # skip ahead
    fast_forward(120)
    s = hsm_status()
    assert 'period_ends' not in s
    assert 'has_spend' not in s

    amt = 0.30E8
    psbt = fake_txn(1, 2, dev.master_xpub, outvals=[amt, 1E8-amt], change_outputs=[1])
    attempt_psbt(psbt)      # 1/3rd of limit
    attempt_psbt(psbt)      # 1/3rd of limit
    attempt_psbt(psbt)      # 1/3rd of limit
    attempt_psbt(psbt, 'would exceed period spending')

    s = hsm_status()
    assert 90 <= s.period_ends <= 120
    assert s.has_spent == [int(amt*3)]


def test_user_subset(dev, start_hsm, tweak_rule, load_hsm_users, fake_txn, attempt_psbt, auth_user):
    psbt = fake_txn(1,1, dev.master_xpub)
    auth_user.psbt_hash = sha256(psbt).digest()

    policy = DICT(rules=[dict(users=['totp'])])
    load_hsm_users()
    start_hsm(policy)

    for name in USERS:
        tweak_rule(0, dict(users=[name]))

        # should fail
        auth_user(name, garbage=True)
        msg = attempt_psbt(psbt, ': mismatch')
        assert name in msg
        assert 'wrong auth' in msg

        # should work
        auth_user(name)
        attempt_psbt(psbt)

        # auth should be cleared
        attempt_psbt(psbt, 'need user(s) confirmation')

        # fail as "replay"
        # - except PW thing is linked to PSBT, not the counter
        # - except HOTP doesn't see it as replay because it doesn't even check old counter value
        if name != 'pw':
            auth_user(name, do_replay=True)
            attempt_psbt(psbt, 'replay' if name == 'totp' else 'mismatch')

def test_min_users_parse(dev, start_hsm, tweak_rule, load_hsm_users, 
                            auth_user, sim_exec, readback_rule):

    policy = DICT(rules=[dict(users=USERS)])
    load_hsm_users()
    start_hsm(policy)

    r = readback_rule(0)
    assert sorted(r.users) == sorted(USERS)
    assert r.min_users == len(USERS)

    for n in range(1, len(USERS)-1):
        policy = DICT(rules=[dict(users=USERS, min_users=n)])
        tweak_rule(0, policy.rules[0])
        r = readback_rule(0)
        assert sorted(r.users) == sorted(USERS)
        assert r.min_users == n if n else r.min_users == len(USERS)

    policy = DICT(rules=[dict(users=USERS, min_users=0)])
    with pytest.raises(RuntimeError) as ee:
        tweak_rule(0, policy.rules[0])
    assert 'must be in range' in str(ee)

    policy = DICT(rules=[dict(users=USERS, min_users=7)])
    with pytest.raises(RuntimeError) as ee:
        tweak_rule(0, policy.rules[0])
    assert 'must be in range' in str(ee)

    policy = DICT(rules=[dict(users=USERS+USERS+USERS, min_users=7)])
    with pytest.raises(RuntimeError) as ee:
        tweak_rule(0, policy.rules[0])
    assert 'dup users' in str(ee)


def test_min_users_perms(dev, quick_start_hsm, load_hsm_users, fake_txn,
                            attempt_psbt, auth_user, sim_exec, readback_rule):
    psbt = fake_txn(1,1, dev.master_xpub)
    auth_user.psbt_hash = sha256(psbt).digest()

    load_hsm_users()

    # all subsets of users
    for n in range(1, len(USERS)):
        policy = DICT(rules=[dict(users=USERS, min_users=n)])
        quick_start_hsm(policy)

        for au in itertools.permutations(USERS, n):
            #print("Auth with: " + '+'.join(au))
            for u in au:
                auth_user(u)

        attempt_psbt(psbt)

        # auth should be cleared
        attempt_psbt(psbt, 'need user(s) confirmation')

def calc_local_pincode(psbt_sha, next_local_code):
    from binascii import a2b_base64
    import struct, hmac

    key = a2b_base64(next_local_code)
    assert len(key) >= 15
    assert len(psbt_sha) == 32
    digest = hmac.new(key, psbt_sha, sha256).digest()

    num = struct.unpack('>I', digest[-4:])[0] & 0x7fffffff
    return '%06d' % (num % 1000000)
        
def test_local_conf(dev, quick_start_hsm, tweak_rule, load_hsm_users, fake_txn, enter_local_code,
                            hsm_status, attempt_psbt, auth_user, sim_exec, readback_rule):
    
    psbt = fake_txn(1,1, dev.master_xpub)
    auth_user.psbt_hash = sha256(psbt).digest()

    # self test vectors
    assert calc_local_pincode(b'b'*32, 'YWFhYWFhYWFhYWFhYWFh') == '998170'
    assert calc_local_pincode(bytes(32), 'YWFhYWFhYWFhYWFaYWFh') == '816912'

    load_hsm_users()
    policy = DICT(rules=[dict(users=USERS, local_conf=True)])
    s = quick_start_hsm(policy)

    for u in USERS:
        auth_user(u)

    lcode = calc_local_pincode(sha256(psbt).digest(), s.next_local_code)
    enter_local_code(lcode)
    attempt_psbt(psbt)

    for u in USERS:
        auth_user(u)
    attempt_psbt(psbt, 'local operator didn\'t confirm')

    tweak_rule(0, dict(local_conf=True))
    attempt_psbt(psbt, 'local operator didn\'t confirm')

    s = hsm_status()
    lcode = calc_local_pincode(sha256(psbt).digest(), s.next_local_code)
    enter_local_code(lcode)
    attempt_psbt(psbt)

def worst_case_policy():
    MAX_NUMBER_USERS = 30       # from shared/users.py
    from helpers import prandom
    from base64 import b32encode

    users = {f'user{i:02d}': [1, b32encode(prandom(10)).decode('ascii'), 0]
                for i in range(MAX_NUMBER_USERS)}

    paths = [f'm/{i}p/{i+3}' for i in range(10)]

    addrs = [render_address(b'\x00\x14' + prandom(20)) for i in range(5)]

    p = DICT(period=30, share_xpubs=paths, share_addrs=paths+['p2sh'], msg_paths=paths,
                warnings_ok=False, must_log=True)
    p.rules = [dict(
                        local_conf=True, 
                        whitelist = addrs,
                        users = list(users.keys()),
                        min_users = rn+3,
                        max_amount = int(1E10),
                        per_period = int(1E10),
                        wallet = '1') 
                for rn in range(3) ]

    return users, p

def test_worst_policy(start_hsm, load_hsm_users):
    # biggest possible HSM config?
    users, policy = worst_case_policy()
    load_hsm_users(users)
    start_hsm(policy)

@pytest.mark.parametrize('case', ['simple', 'worst'])
def test_backup_policy(case, unit_test, start_hsm, load_hsm_users):
    # exercise dump of backup data

    if case == 'simple':
        policy = DICT(rules=[dict()])
        load_hsm_users()
    elif case == 'worst':
        users, policy = worst_case_policy()
        load_hsm_users(users)

    start_hsm(policy)

    unit_test('devtest/backups.py')

# KEEP LAST -- can only be run once, crashes device
@pytest.mark.onetime
def test_max_refusals(attempt_msg_sign, start_hsm, hsm_status, threshold=100):
    start_hsm({})

    assert hsm_status().refusals == 0

    for i in range(threshold):
        attempt_msg_sign('signing not permitted', b'msg here', 'm/73')

    assert hsm_status().refusals == threshold

    # CC will reboot itself
    time.sleep(.5)

    with pytest.raises(BaseError) as ee:
        attempt_msg_sign('signing not permitted', b'msg here', 'm/73', timeout=1000)
    assert ('timeout' in str(ee)) or ('read error' in str(ee))

# - never_log


    
# EOF
