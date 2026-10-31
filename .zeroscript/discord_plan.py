import json, sys, time, urllib.request as u, urllib.error as ue, urllib.parse

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CFG = json.load(open('config.json', encoding='utf-8'))
ENV = CFG['mcpServers']['discord']['env']
TOKEN = ENV['DISCORD_BOT_TOKEN']
GUILD = ENV.get('ZS_DISCORD_GUILD_IDS', '').split(',')[0].strip()
API = 'https://discord.com/api/v10'
UA = 'DiscordBot (https://github.com/zeroscript, 0.1.0)'


def patch(cid, body, reason=None):
    data = json.dumps(body).encode('utf-8')
    h = {'Authorization': 'Bot ' + TOKEN, 'User-Agent': UA, 'Content-Type': 'application/json'}
    if reason:
        h['X-Audit-Log-Reason'] = urllib.parse.quote(reason[:512])
    for _ in range(5):
        req = u.Request(f'{API}/channels/{cid}', data=data, headers=h, method='PATCH')
        try:
            with u.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except ue.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode('utf-8')).get('retry_after', 2))
                except Exception:
                    wait = 2.0
                print(f'   429 on {cid}, waiting {wait:.1f}s')
                time.sleep(min(wait, 15))
                continue
            raise RuntimeError(f'HTTP {e.code}: ' + e.read().decode('utf-8', 'replace')[:200])
    raise RuntimeError('rate limited too many times')


EDITS = [
    ('1559724986195910750', {'name': '\U0001F50A Voice'}),
    ('1559724986195910752', {'name': 'General Voice'}),
    ('1560921911771402310', {'name': 'game-links', 'topic': 'Share and find games to play.'}),
    ('1559724986195910751', {'topic': 'General conversation for the GameboyEMU Max community.'}),
    ('1559725234213359697', {'topic': 'Screenshots, clips, and creations. Keep it on-topic.'}),
    ('1559725040961069108', {'topic': 'Official news and releases.'}),
    ('1559725050196664340', {'topic': 'Patch notes and development progress.'}),
    ('1559725080433402016', {'topic': 'Early looks at what is coming next.'}),
]

print(f'Applying {len(EDITS)} edits to guild {GUILD}')
for i, (cid, body) in enumerate(EDITS, 1):
    try:
        ch = patch(cid, body, 'Professionalize channel structure')
        print(f'{i}. OK   {cid} name={ch.get("name")!r} topic={(ch.get("topic") or "")[:45]!r}')
    except Exception as e:
        print(f'{i}. FAIL {cid}: {e}')
    time.sleep(1.2)

req = u.Request(f'{API}/guilds/{GUILD}/channels',
                headers={'Authorization': 'Bot ' + TOKEN, 'User-Agent': UA})
with u.urlopen(req, timeout=30) as r:
    chans = json.loads(r.read().decode('utf-8'))

print('\n--- FINAL STATE ---')
for c in sorted(chans, key=lambda x: (x.get('parent_id') or '', x.get('position', 0))):
    kind = {0: 'text', 2: 'voice', 4: 'category'}.get(c.get('type'), str(c.get('type')))
    print(f'{kind:9} {c.get("name")!r:30} topic={(c.get("topic") or "")[:55]!r}')
