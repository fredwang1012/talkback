"""General, book-agnostic synonym map for query expansion in local (BM25-only) retrieval.

Spoken questions use everyday words ("weird", "big", "sad", "mean"); novels use literary ones ("queer", "tall",
"sorrowful", "cruel"). Each entry maps a conversational word to a few related words, strongest first. Expansions are
down-weighted by the caller (see retrieval.EXPAND_WEIGHT) so exact matches always dominate. No names or plot terms.
"""
import re

STOPWORDS = frozenset("""a an the of to and in on at for with as by from into onto about over under up down out off
is are was were be been being am do does did done have has had having will would shall should can could may might must
i me my mine we us our you your he him his she her it its they them their this that these those there here
what who whom whose which when where why how not no yes so if then than too very just also only any some all each
one ones thing things something anything really actually kind sort lot much many more most""".split())

_RAW = """
big: large huge giant enormous tall vast grow
bigger: larger taller grow
huge: enormous giant vast great
large: big huge great tall
giant: enormous huge tall
tall: high height feet
small: little tiny short shrink
smaller: shrink little tiny
tiny: little small minute
little: small tiny
short: small little
shrink: shrank smaller small
grow: grew growing larger bigger taller
grew: grow larger bigger taller
size: tall high feet inches small large
height: tall high feet
fat: stout plump round
thin: slim lean slender
sad: sorrowful unhappy miserable weeping tears mournful
unhappy: sad miserable sorrowful
cry: wept weep tears sob sobbing
crying: wept weeping tears sobbing
cried: wept tears sobbed
weep: wept cry tears
upset: distressed troubled unhappy
scared: afraid frightened terrified fear
afraid: frightened scared terrified fear
frightened: afraid scared terrified
fear: afraid terror dread frightened
terrified: frightened afraid horror
angry: furious cross fury rage enraged
mad: angry furious cross crazy
furious: angry rage fury enraged
annoyed: cross irritated vexed angry
happy: glad delighted cheerful joyful pleased
glad: happy pleased delighted
excited: eager thrilled delighted
nervous: anxious uneasy worried
worried: anxious troubled uneasy
anxious: nervous worried uneasy
surprised: astonished amazed startled
shocked: astonished startled amazed horrified
confused: puzzled bewildered perplexed
puzzled: confused bewildered perplexed
lonely: alone solitary
alone: lonely solitary
tired: weary sleepy exhausted
sleepy: drowsy tired
hungry: starving famished
embarrassed: ashamed blushed
jealous: envy envious
brave: courage courageous bold daring
bold: brave daring courage
feel: felt feeling
felt: feel feeling
feeling: felt emotion
love: adore fond affection
hate: loathe despise detest
worry: anxious fret trouble
laugh: laughed giggled chuckled amused
laughing: laughed giggled amused
funny: amusing comical absurd laughed strange
smile: smiled grinned grin
say: said replied remarked
said: replied remarked exclaimed
tell: told said explain
told: said explained
ask: asked inquired question demanded
asked: inquired demanded questioned
answer: replied responded
reply: answered responded said
shout: shouted yelled cried screamed
shouting: shouted yelled screamed
yell: shouted screamed cried
scream: screamed shrieked shouted
whisper: whispered murmured muttered
talk: spoke said conversation
speak: spoke said talk
spoke: said talked
call: called named shouted
name: called named
explain: explained said told
mention: said spoke
warn: warned cautioned
promise: vowed swore
agree: consented
refuse: declined
argue: quarrel dispute
quarrel: argue dispute
walk: walked strolled wandered
run: ran hurried rushed raced
ran: run hurried rushed fled
hurry: hurried rushed ran
rush: hurried raced ran
flee: fled escape ran
escape: fled flee ran
fall: fell falling dropped tumbled
fell: fall dropped tumbled
falling: fell dropped tumbled
drop: fell dropped
jump: leapt sprang leaped
climb: climbed scrambled
arrive: came reached
enter: entered
follow: followed chased
chase: chased followed pursued
hide: hid hiding concealed
hid: hiding concealed
find: found discovered
found: discovered
watch: watched observed
stare: stared gazed
notice: noticed saw
meet: met encounter
met: encounter
wake: woke awake
woke: awake awoke
sleep: slept asleep dream
dream: dreamed dreaming asleep
die: died dead death
dead: died death
death: died dead
kill: killed murder slain
killed: murdered slain dead
fight: fought battle quarrel
battle: fought fight
attack: attacked assault
save: saved rescue rescued
rescue: saved
help: helped assist aid
steal: stole stolen thief
stole: stolen thief
thief: stole stolen
become: became turn
turn: turned became change
turned: became changed
change: changed became turn transform
changed: became transform
transform: turned became change
happen: happened occurred
happened: occurred became
occur: happened
end: ended finally last
ended: finish finally
finish: ended finally
begin: began start
start: began started
began: start
stop: stopped ceased
try: tried attempted
tried: attempted
want: wished desired
wish: wanted desired
need: required
decide: decided resolved
plan: scheme plot
think: thought wondered
thought: wondered
wonder: wondered curious
know: knew understood
remember: remembered recalled
forget: forgot
understand: understood
believe: believed
learn: learned taught
teach: taught lesson
lesson: taught learn
weird: strange queer curious odd peculiar
strange: queer curious odd peculiar
odd: strange queer curious peculiar
crazy: mad foolish
silly: foolish absurd stupid
stupid: foolish silly idiot
nonsense: absurd foolish rubbish
absurd: nonsense ridiculous silly
mean: cruel rude unkind nasty
rude: impolite insolent unkind
cruel: unkind harsh mean
nasty: horrid unpleasant unkind
nice: kind pleasant polite
polite: courteous civil
bad: wicked evil naughty
evil: wicked dark villain
wicked: evil bad
villain: evil wicked enemy
enemy: foe rival
friend: companion ally
ally: friend companion
smart: clever intelligent wise
clever: smart wise intelligent
wise: clever sensible
foolish: silly stupid
beautiful: lovely pretty fair
pretty: beautiful lovely
ugly: hideous horrid
lovely: beautiful pretty delightful
scary: frightening terrible dreadful fear
dangerous: perilous risky
magic: spell enchanted wizard
magical: enchanted spell
spell: charm magic enchantment
strong: powerful mighty
powerful: strong mighty
weak: feeble frail
fast: quickly swift rapid
quickly: swiftly hurriedly fast
slowly: gradually
sudden: suddenly abrupt
loud: noisy
quiet: silent hushed still
silent: quiet hushed
dark: gloom shadow
bright: shining light
cold: chilly freezing
hot: warm heat
wet: soaked damp drenched
dry: dried
trick: trap deceive fool deceived
lie: lied untrue false
liar: lied deceive
secret: hidden mystery
mystery: puzzle secret
trouble: danger difficulty
problem: trouble difficulty puzzle
reason: because cause
mistake: error wrong
wrong: mistake error
fix: mend repair
break: broke smashed
broke: smashed shattered
open: opened unlock
close: closed shut
shut: closed
enormous: huge vast giant
mother: mum mama
mum: mother
father: dad papa
dad: father
parent: mother father
brother: sibling
sister: sibling
child: kid boy girl
kid: child boy girl
baby: infant child
woman: lady
lady: woman
king: ruler monarch royal
queen: ruler monarch royal
ruler: king queen monarch
leader: chief captain
captain: leader chief
servant: maid attendant
maid: servant housemaid
animal: creature beast
creature: beast animal monster
monster: beast creature
beast: creature monster
ghost: spirit phantom
drink: drank potion
drank: drink
party: feast celebration gathering
dance: ball dancing
song: sing sang verse
sing: sang song
poem: verse rhyme
fire: flames blaze
money: gold coins
letter: note message
message: note letter
trial: court judge
court: trial judge
punish: punishment sentence
punishment: sentence penalty
prison: jail captive
prisoner: captive jail
rule: law
law: rule
fly: flew flying
flew: flying
swim: swam
ride: rode
travel: journey voyage
journey: travel trip
road: path way
door: gate entrance
garden: grounds
forest: woods trees
sea: ocean
"""

SYNONYMS: dict[str, tuple[str, ...]] = {}
for _line in _RAW.strip().splitlines():
    _k, _, _v = _line.partition(":")
    _k = _k.strip()
    _new = tuple(w for w in _v.split() if w not in STOPWORDS and w != _k)
    SYNONYMS[_k] = tuple(dict.fromkeys(SYNONYMS.get(_k, ()) + _new))  # duplicate keys merge

MAX_EXPANSIONS_PER_TOKEN = 3


def is_stopword(tok: str) -> bool:
    return tok in STOPWORDS


def _lookup(tok: str) -> tuple[str, ...]:
    if tok in SYNONYMS:
        return SYNONYMS[tok]
    for suf in ("ing", "ed", "es", "s"):  # crude inflection fallback: "shouts" -> "shout"
        if tok.endswith(suf) and len(tok) - len(suf) >= 3 and tok[: -len(suf)] in SYNONYMS:
            return SYNONYMS[tok[: -len(suf)]]
    return ()


def expand_tokens(tokens: list[str], max_per_token: int = MAX_EXPANSIONS_PER_TOKEN) -> list[str]:
    """Extra (non-original) query tokens related to `tokens`. Stopwords are never expanded; each token adds at most
    `max_per_token` words; words already in the query are not repeated. Order follows the query."""
    have = set(tokens)
    out: list[str] = []
    for t in tokens:
        if is_stopword(t):
            continue
        for w in _lookup(t)[:max_per_token]:
            if w not in have:
                have.add(w)
                out.append(w)
    return out
