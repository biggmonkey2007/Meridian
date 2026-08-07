# -*- coding: utf-8 -*-
"""
Meridian regression tests — run this after ANY change to classification or geolocation.

    python test_meridian.py

Every case below is a bug that actually shipped. The point is that fixing one thing must never
silently break another, so each fix leaves a permanent test behind it.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mapp", os.path.join(HERE, "app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


# (headline, expected_category, why_this_case_exists)
CATEGORY_CASES = [
    ("Leandro Trossard set for Besiktas move after Arsenal agree deal with Turkish side", "sports",
     "SHIPPED BUG: scored ZERO and fell to the 'politics' default, so the sports filter (which drops "
     "transfer chatter) never saw it — a transfer rumour sat on the map as a POLITICS dot on Singapore"),
    ("Russian forces shell Toretsk overnight", "security",
     "SHIPPED BUG: 'shell' was not a security keyword at all — the most basic war verb we have"),
    ("Victorian teachers set to strike again following deadlocked negotiations", "politics",
     "SHIPPED BUG: filed under CONFLICT & SECURITY. The labour-strike mask demanded the words be "
     "ADJACENT, so 'teachers SET TO strike' sailed through"),
    ("Romania votes in tense presidential election", "politics",
     "GUARD: category matching is plain SUBSTRING — the club 'Roma' is inside 'Romania'"),
    ("McIlroy calls for golf major season to be stretched out", "sports",
     "SHIPPED BUG: GOLF had no keywords at all — it defaulted to politics"),
    ("Wales call up two replacements as they sweat on skipper Lake", "sports",
     "SHIPPED BUG: a rugby squad announcement defaulted to politics"),
    ("Satellite imagery confirms three fuel storage tanks burned at the Tverneftteprodukt oil depot in Tver after the July 9 overnight strike",
     "security", "SHIPPED BUG: 'satellite' won first-match and filed a war story under Science & Tech"),
    ("Russian drones struck an oil refinery in Omsk overnight", "security", "drone strike"),
    ("Israeli airstrike kills 3 in southern Lebanon", "security", "airstrike + casualties"),
    ("Three Iranian ballistic missiles impacted the Shuwaikh Port in Kuwait City", "security", "ballistic missile"),
    ("China ballistic missile test pushes case for Pacific defences", "security", "missile test"),
    ("SpaceX launches a reusable rocket into orbit", "tech", "genuine space story stays tech"),
    ("Apple sues OpenAI for allegedly stealing trade secrets", "tech", "OpenAI"),
    ("AI companies want to water down Australia's copyright rules", "tech", "AI, no conflict words"),
    ("Rail workers strike over pay across the network", "economy", "a labour strike is NOT a military one"),
    ("Wildfires in Spain kill 12 as thousands evacuate", "climate", "'kill' must not beat 'wildfire'"),
    ("Typhoon makes landfall in China as millions flee", "climate", "typhoon"),
    ("Venezuela quake death toll passes 4,300", "climate", "'death toll' must not beat 'quake'"),
    ("Measles outbreak spreads in the capital", "health", "outbreak"),
    ("Inflation climbs as the central bank holds interest rates", "economy", "economy"),
    ("England beat Norway to reach the World Cup semis", "sports", "world cup"),
    ("Moldova's president nominates Vasile Tofan as prime minister", "politics", "nomination"),
    ("Parliament votes to approve the coalition deal", "politics", "parliament"),
    ("UK police say no evidence of political motive in the murder", "society", "crime"),
    ("Mathieu Van der Poel overcomes heat to win Tour de France stage", "sports",
     "SHIPPED BUG: cycling had no keywords, so it defaulted to politics"),
    ("Bellingham scores twice to lift England past Haaland and Norway", "sports",
     "SHIPPED BUG: 'scores' was not a sports word, so it defaulted to politics"),
    ("Democrats split as Israel's war in Gaza dominates US midterms", "politics",
     "SHIPPED BUG: a bare ' war ' beat an election story"),
    ("Gasoline in occupied Crimea has reached 450 rubles per liter, 6 times the Russian retail price",
     "economy", "SHIPPED BUG: a fuel-price story was filed under Society"),
    ("Imagery also shows significant damage to the AVT-6 crude distillation unit at the Syzran oil refinery.",
     "security", "SHIPPED BUG: a strike DAMAGE report has no 'strike' word -> fell to the politics default"),
    ("Storm damage across Florida as thousands lose power", "climate",
     "'damage' is a security word — a STORM must still win"),
    ("At least 27 dead in Bangkok pub fire", "climate",
     "SHIPPED BUG: a deadly fire scored 0 and fell to the politics default"),
    ("Russian forces set fire to one of the buildings of the Kherson State Maritime Academy, "
     "Vice-Rector Oleksandr Shumei said.", "security",
     "SHIPPED BUG: filed under CLIMATE. Security scored ZERO — 'forces' was not a word we knew — "
     "while climate scored 1 on 'fire'. An army burning a building is an ATTACK; the arson wording "
     "('set fire') is what separates it from a real accidental fire"),
    ("France arrests arson suspects amid Fontainebleau forest fire", "climate",
     "GUARD: a real forest fire must STAY climate even though arson is mentioned"),
    ("Firefighters battle flames in Fontainebleau historic forest", "climate",
     "GUARD: firefighting a forest blaze is climate, not conflict"),
    ("Market forces drive inflation higher as the central bank holds rates", "economy",
     "GUARD: 'market forces' must not leak into security via the military 'forces' keyword"),
]

# (headline, summary, expected_place_substring, why)
GEO_CASES = [
    ("Bellingham scores twice to lift England past Haaland's Norway", "", "England",
     "SHIPPED BUG: gazetteer read the surname as Bellingham, Washington"),
    # BATCH of wrong-continent dots: common words / acronyms read as towns, and ambiguous names that picked
    # the wrong country. Curated the real scene (Sizewell, Beaufort Castle); guarded acronyms + common words.
    ("Wildfire near Sizewell nuclear plant causes 'total devastation' to Suffolk landscape",
     "Wildfire on Suffolk's Dunwich Heath has burned over 150 hectares including the Minsmere nature reserve.",
     "United Kingdom", "SHIPPED: 'Suffolk' dotted Suffolk, Virginia; the scene is Sizewell on the English coast"),
    ("Lebanese president says Israeli blasts at Beaufort Castle threaten framework agreement",
     "Lebanese President Joseph Aoun condemned Israeli attacks in southern Lebanon, citing blasts at Beaufort Castle.",
     "Lebanon", "SHIPPED: 'Beaufort' dotted Beaufort, Malaysia; Beaufort Castle is in southern Lebanon"),
    ("Satellite images reveal the clearest picture yet of damage to the Saudi Aramco oil refinery in Jazan",
     "Satellite images show the worst damage yet to a Saudi oil refinery hit by drone and missile attacks. The refinery in Jazan was targeted by the Houthis.",
     "Saudi Arabia", "SHIPPED: 'Jazan' dotted a tiny Iranian village (pop 1,818); the Saudi city is in GeoNames only as 'Jizan'"),
    ("Record drought and heatwave bring energy emergency to Eastern Europe",
     "Governments across Central and Eastern Europe conserve electricity amid record low water on the Danube River. In Hungary, the country's only nuclear plant is being shut down as there is not enough water to cool its reactors.",
     "Hungary", "SHIPPED: 'Central' (from 'Central and Eastern Europe') dotted Central, Ontario, Canada; the scene is the Danube / Hungary"),
    ("Statue of Yoni Netanyahu unveiled at Uganda's Entebbe Airport, 50 years after raid",
     "A statue of Yonatan Netanyahu, the Israeli brother of PM Benjamin Netanyahu, has been unveiled at the old Entebbe Airport terminal in Uganda.",
     "Uganda", "SHIPPED: dotted ISRAEL — 'Uganda's' pushed the 'at' out of the located window, and the "
     "Netanyahu statement-country hijacked the genuine Entebbe city scene"),
    ("Israeli officer moderately hurt in overnight clash with Hezbollah gunmen",
     "An Israeli officer was hurt in a clash with Hezbollah gunmen overnight. The clash happened in the Ali Taher Ridge area.",
     "Lebanon", "SHIPPED: dotted ISRAEL (the actor); the only place named is the Ali Taher ridge in southern Lebanon, absent from the city gazetteer"),
    ("A Jewish MK on Ra'am's slate could reshape the anti-Netanyahu coalition",
     "Ra'am polled 750 Arab citizens; a Jewish MK from Yesh Atid could join the slate.",
     "Israel", "SHIPPED: dotted 'Arab, United States' (Arab, Alabama). 'Arab' in the news is the demonym, never the town"),
    ("A Turkish UAV carried out an overflight over the village of Farmakonisi",
     "A Turkish drone flew over the Greek village of Farmakonisi in the Aegean.",
     "Greece", "SHIPPED: dotted 'The Village, United States'; Farmakonisi is a Greek islet absent from the gazetteer"),
    ("Clashes erupt between Ansarullah and the Yemeni National Resistance Forces",
     "Fighting is ongoing west of the al-Barah heights in the Hays direction, southwest Yemen.",
     "Yemen", "SHIPPED: dotted 'Hays, United States' (Hays, Kansas); Hays is an active front in southwest Yemen"),
    ("How extreme weather is shaping tomorrow's electric grid",
     "Severe heat waves across 17 US states strain the national electric grid, with Texas a key testing ground.",
     "United States", "SHIPPED: the gerund 'shaping' dotted the town of Shaping, China"),
    ("HIV prevention drug could reduce cases globally but USAID cuts prevent access, say experts",
     "A new HIV prevention drug could cut new cases in South Africa, Zimbabwe and Kenya, but US aid cuts hinder access.",
     "!Iran", "SHIPPED: the acronym 'HIV' dotted the village of Hiv, Iran"),
    ("EU dismisses speculation over Schengen action against Spain as Ceuta crisis unfolds",
     "Spain's mass irregular border crossings into the enclave of Ceuta have sparked EU caution over Schengen action.",
     "Spain", "SHIPPED: 'Schengen' dotted the Luxembourg village; the story is Ceuta, Spain"),
    ("Over $1.1 trillion added to the US stock market today.",
     "Over $1.1 trillion was added to the US stock market as indices rallied.",
     "United States", "SHIPPED: the word 'Over' dotted the village of Over, UK"),
    ("Rescue crews are responding to an F-35 crash at Miramar Air Base near San Diego",
     "A US F-35 fighter jet crashed and exploded at Miramar Air Base near San Diego.",
     "Miramar Air Base", "SHIPPED: 'Miramar' dotted Miramar, Florida (Miami); MCAS Miramar is in San Diego"),
    ("Western regime change wars led to Spain's migrant crisis",
     "A massive influx of migrants entered the Spanish city of Ceuta from Morocco. A Russian diplomat said regime change wars in Iraq and Libya destabilized the region.",
     "Spain", "SHIPPED: dotted IRAQ (a background country the piece blames); the scene is Ceuta, Spain"),
    ("Zelensky said Ukrainian forces struck targets in the Black and Azov seas overnight",
     "Ukrainian forces hit ships in the Black and Azov seas. Separately, drones struck an oil refinery in Ufa, Bashkortostan, some 1,400 km away.",
     "Azov Sea", "SHIPPED: dotted Kyiv (the speaker) then Ufa (a later roundup strike). The collapsed plural "
     "'Black and Azov seas' matched no singular sea key; expand it, and among two named seas the enclosed Azov wins"),
    ("Ukrainian drones struck ships in the Baltic and North seas", "",
     "Sea", "the coordinated-plural expansion is general: two real seas, so it must land on water, not a town"),
    # A country taking a DOMESTIC action (orders/expels/bans/sanctions) is news at its OWN seat; a country
    # named only as background must not steal the dot.
    ("France orders the expulsion of Russian journalist Xenia Fedorova, former director of RT France",
     "RT France was shut down in 2023, a year after the start of the Russian Intervention in Ukraine, but Ms. Fedorova remained.",
     "France", "SHIPPED BUG: France ORDERED the expulsion; the dot belongs in France"),
    ("France orders the expulsion of Russian journalist Xenia Fedorova, former director of RT France",
     "RT France was shut down in 2023, a year after the start of the Russian Intervention in Ukraine, but Ms. Fedorova remained.",
     "!Ukraine", "SHIPPED BUG: a background 'Intervention in Ukraine' dotted Kyiv"),
    # A national official SPEAKING/TESTIFYING about a foreign country is news in THEIR country, not the
    # topic country. These dotted Tehran because "Iran" was the only place named.
    ("Trump to attend dignified transfer of fallen soldiers. And, Hegseth testifies on Iran",
     "Pete Hegseth is requesting billions from Congress for the rising cost of the war in Iran.",
     "United States",
     "SHIPPED BUG: a US official testifying ON Iran dotted Tehran — Iran is the topic, the event is in the US"),
    ("The Iran war has cost the US $37.5 billion dollars, says Defense Secretary Pete Hegseth", "",
     "United States",
     "SHIPPED BUG: trailing attribution ('says Defense Secretary Hegseth') dotted Tehran, not Washington"),
    ("Israel's new exhibit exposes years of Hamas terrorist planning before Oct 7 massacre",
     "Documents reveal Hezbollah and Iran's roles in the Oct. 7 assault.", "Israel",
     "the exhibit is IN Israel; Iran named in the body is the topic, not the scene"),
    # A WEAPON's nationality is its ORIGIN, not the scene it strikes. The event is where it LANDED.
    ("An Iranian projectile, likely a one-way drone, impacted at Ali al-Salem Air Base, Kuwait.", "",
     "Kuwait",
     "SHIPPED BUG: 'Iranian' (leftmost) beat the explicit impact country — a drone's flag is not a location"),
    ("Russian missile strikes apartment block in Kyiv", "", "Kyiv",
     "the missile's origin (Russia) is not the scene; it struck Kyiv"),
    ("North Korean rocket debris recovered in South Korea", "", "Korea",
     "the rocket is North Korean but was recovered IN South Korea"),
    ("Iran's drone shot down over Iraq", "", "Iraq",
     "possessive weapon origin (\"Iran's drone\") must not beat the airspace it was downed over"),
    # Common words / partial names that were landing dots on tiny same-named US/UK towns.
    ("Major fire after Ukrainian strike on St Petersburg warehouse", "", "Saint Petersburg",
     "SHIPPED BUG: 'St Petersburg' fell to Petersburg, Virginia — St must normalise to Saint (Russia)"),
    ("China's largest memory chipmaker sparks fears of a cash drain", "", "China",
     "SHIPPED BUG: 'sparks fears' dotted Sparks, Nevada — it's a verb here, not a place"),
    ("University courses covering Israel-Palestine should be audited", "", "Israel",
     "SHIPPED BUG: 'University' dotted University, Florida — a generic word, never that town"),
    ("Protest at the University of Tehran turns violent", "", "Tehran",
     "the University OF TEHRAN is in Tehran, not the town University, Florida"),
    ("Explosion reported in Sparks, Nevada overnight", "", "Sparks",
     "...but a real Sparks dot is KEPT when the sentence actually locates something there"),
    # ACTOR-vs-SCENE: a strike belongs where it LANDED, not where the attacker sits, and a country's
    # asset abroad sits in its HOST country — these were all dotting the United States.
    ("Heavy explosions as US pounds Iranian city", "", "Iran",
     "SHIPPED BUG: 'US pounds Iranian city' dotted the US — the scene is the Iranian city"),
    ("Iran military says attacked US bases in Bahrain, Jordan, Kuwait", "", "Bahrain",
     "SHIPPED BUG: 'US bases in Bahrain' dotted the US — the bases sit IN the host country"),
    ("US Embassy in Beirut evacuated after threat", "", "Beirut",
     "a country's embassy is in its host city (Beirut), not back home"),
    # regression guards for the above — these must keep working
    ("Gaza strikes continue for third day", "", "Gaza",
     "'Gaza strikes' (strikes as a NOUN) is still the scene Gaza, not sunk as an attacker"),
    ("Russia strikes Ukrainian port of Odesa", "", "Odesa",
     "the attacker (Russia) sinks; the struck Ukrainian port is the scene"),
    ("Why Iran's Pickaxe Mountain is a tough target for the US", "", "Pickaxe Mountain",
     "a named nuclear site (Pickaxe Mountain / Kuh-e Kolang Gaz La by Natanz) plots THERE, not the country centroid"),
    # A US leader/institution ACTING or DELIBERATING on Iran as the topic is a Washington story, not Tehran.
    ("Pentagon lowers official Iran war death toll, omitting four killed this month", "", "United States",
     "SHIPPED BUG: the Pentagon announcing a figure dotted Tehran — it's a Washington story"),
    ("Trump considers 'massive attack' on Iran as tensions rise", "", "United States",
     "SHIPPED BUG: a leader only CONSIDERING a strike dotted the target — it's news at their seat"),
    ("Trump visits Israel for a peace summit", "", "Israel",
     "...but a leader VISITING a place is AT that place (going-verbs are not deliberation)"),
    ("Rubio says the US is ready to help end the war in Ukraine", "", "United States",
     "a US official's STATEMENT about a foreign country is at their seat, even when the topic is 'located'"),
    ("Netanyahu says Israel will respond to any attack", "", "Israel",
     "...but the official's OWN country is never overridden away from itself"),
    ("Israeli shelling targets southern Lebanon despite the US-mediated framework", "", "Lebanon",
     "an incidental 'US' in context must NOT demote the COUNTRY Lebanon to Lebanon, Tennessee (pop 30k)"),
    ("Trump wins Georgia in a tight race", "", "United States",
     "...but a PROMINENT same-named US region (Georgia the state, pop 5M) DOES win in US context"),
    ("Singapore tightens monetary policy in a surprise move", "", "Singapore",
     "'surprise' is the common noun — never a dot on Surprise, Arizona, even after 'in a'"),
    ("Honor Magic V6 review: sleek foldable phone lacking a bit of polish", "", "!Poland",
     "SHIPPED BUG: 'a bit of polish' (shine) dotted Poland — a place-word that's also an everyday word "
     "needs a capital to count"),
    ("Quitting smoking cold turkey is hard after twenty years", "", "!Turkey",
     "'cold turkey' the idiom is not Turkey the country"),
    ("Polish PM Tusk addresses parliament in Warsaw", "", "Poland",
     "...but capitalised 'Polish' IS still the demonym for Poland"),
    ("Ukraine's National Guard struck a Russian Buk-M3 and S-300 air defense system on the Kostiantynivka front",
     "", "Kostiantynivka",
     "SHIPPED BUG: the nationality of a destroyed weapon SYSTEM ('Russian ... air defense system') is not "
     "the scene — the front it was struck on is"),
    ("A drone strike hit a hotel in the town of Kyrylivka overnight", "", "Kyrylivka",
     "a ~1,400-person town named only in the body now pins exactly (GeoNames small-town coverage + 'town of')"),
    ("Meta used AI to target older workers, lawsuit alleges", "", "!Meta",
     "a tiny same-named town ('Meta', Italy) must NOT dot a company — tiny towns need explicit location"),
    # A city name that is ALSO a surname/forename, sitting inside a person's name with no locational
    # context, is the PERSON — not a dot on a same-named town. These are the exact bugs the exe made.
    ("Mistakenly deported man Abrego Garcia returns to US to face charges", "", "United States",
     "SHIPPED BUG: surname 'Garcia' dotted Garcia, Mexico over the US he returns to"),
    ("Nancy Pelosi and 197 Democrats voted against the Stop Insider Trading act", "", "United States",
     "SHIPPED BUG: forename 'Nancy' dotted Nancy, France — she is a US legislator"),
    # ...but the town is KEPT when the sentence actually locates it there (preposition = real place).
    ("Fierce fighting reported in Bakhmut as Russian forces advance", "", "Bakhmut",
     "a real small-town scene with a locational preposition must NOT be vetoed as a name"),
    # 'Georgia' is a US state AND a country — a named national leader puts their country in context so it
    # resolves at home.
    ("President Trump in Georgia: people trying to bring in drugs by sea are the bravest in history", "",
     "United States", "SHIPPED BUG: a Trump rally in Georgia the US STATE dotted the Caucasus country"),
    ("Protests erupt in Tbilisi as Georgia debates an EU accession bill", "", "Tbilisi",
     "GUARD: Tbilisi context keeps Georgia the COUNTRY — the leader rule must not override a real local scene"),
    ("US airstrikes hit Iranian nuclear site at Natanz", "", "Iran",
     "GUARD: a real STRIKE marks its target as the scene — must NOT be dragged to Washington"),
    ("Khamenei says Iran will respond to overnight strikes", "", "Iran",
     "GUARD: Iran's own leader speaking about Iran is Iran news — the actor redirect must not misfire"),
    ("Lindsey Graham dies suddenly, aged 71",
     "The senator died at his home in Washington, US officials said.", "United States",
     "SHIPPED BUG: placeless story fell back to the OUTLET's country (France24 -> France)"),
    ("Ukrainian drone strike hits the Omsk oil refinery", "", "Omsk",
     "NER calls Omsk an ORG; the gazetteer must still win"),
    ("Russian forces shell Toretsk overnight", "", "Toretsk", "NER does not know Toretsk at all"),
    ("Kyiv says Russian drones hit the capital overnight", "", "Kyiv",
     "metonymy: a city CAN 'say' things — must not be vetoed"),
    ("Man arrested in Manchester over stadium plot", "", "Manchester",
     "'arrested' is a person-verb, but 'in' proves it is a place"),
    ("Flash flooding in Missouri leaves one person dead", "", "Missouri", "US states must resolve"),
    ("Hundreds in Sweden protest Israeli attacks in Gaza", "", "Sweden",
     "the subject's location wins, not the topic's"),
    ("Trump threatens to 'decimate' Iran if it tries to kill him", "", "United States",
     "a threat BY an official is located at the official"),
    # --- TARGETS SINK: a sanction/tariff/bill is voted where the body SITS; the country it names
    #     is only what it is aimed at, and nothing has happened there ---
    ("Senate looks to honor Graham with Russia sanctions", "", "Washington",
     "SHIPPED BUG: dotted MOSCOW. The Senate votes in Washington; Russia is only the TARGET"),
    ("Trump signs bill imposing new tariffs on China", "", "Washington",
     "'tariffs ON China' is an act in Washington — the official acts from his own capital"),
    ("EU approves new sanctions on Russia", "", "Brussels",
     "the acting body is the EU, which sits in Brussels"),
    ("House passes Ukraine aid package", "", "Washington",
     "the VOTE is the event; Ukraine is only where the aid would go"),
    ("UK House of Commons backs Russia sanctions", "", "London",
     "GUARD: Britain's lower house must never be read as America's"),
    ("Protesters in Berlin rally against new Russia sanctions", "", "Berlin",
     "GUARD: a REAL scene ('in Berlin') must outrank a sanctions target"),
    ("Nigeria's Senate passes sweeping tax bill", "", "Nigeria",
     "GUARD: a bare 'Senate' must NOT drag the dot to Washington — Nigeria is not a target here"),
    ("Palestinians fly kites in defiance of Israeli siege", "", "Palestine",
     "'in defiance' must not resolve to Defiance, Ohio. NOTE: this case expected ISRAEL until plural "
     "demonyms were added — 'Palestinians' was not a demonym, so the only actor the scanner could see "
     "was the Israeli one. PALESTINE is the right answer (the kites are flown in Gaza); the old "
     "expectation was encoding a gap in the data, not the truth. The Defiance guard is unaffected"),
    ("AI 'actor' Tilly Norwood has a movie coming out", "", None,
     "a surname must not resolve to Norwood, Massachusetts"),
    ("Ukrainian drone strike hit the Odessa port", "", "Ukraine",
     "SHIPPED BUG: the Russian spelling 'Odessa' resolved to Odessa, TEXAS"),
    ("Supermarket in Zaporozhye Region attacked by Ukrainian drone", "", "Zaporozhye",
     "SHIPPED BUG: TASS spellings missed the gazetteer and fell back to Ukraine's capital"),
    ("Russian forces shell Kharkov overnight", "", "Ukraine", "Russian spelling of Kharkiv"),
    # --- context-aware disambiguation (the same NAME, two different places) ---
    ("Valencia floods kill dozens in eastern Spain", "", "Valencia",
     "SHIPPED BUG: NER wrongly tagged 'Valencia' PERSON and vetoed it -> dot landed on Spain's centre"),
    ("Israeli strike hits Tripoli in northern Lebanon", "", "Lebanon",
     "Tripoli is in Libya AND Lebanon — context must choose"),
    ("Tripoli clashes leave 5 dead as militias fight", "Fighting erupted in the Libyan capital.", "Libya",
     "the same name, the other country"),
    # --- facility-level precision (dot on the site, not the city) ---
    ("FP-1 drones hitting the Syzran oil refinery in Russia's Samara region", "", "Syzran Oil Refinery",
     "SHIPPED BUG: 'in RUSSIA's Samara region' grabbed the preposition -> dot on Moscow"),
    ("Ukrainian drone strike hits the Omsk oil refinery", "", "Omsk Oil Refinery", "facility, not city centre"),
    ("Iran strikes tanker in the Strait of Hormuz", "", "Strait of Hormuz", "a dot in the strait itself"),
    # A curated strategic water must resolve even NAKED (no locating preposition to force it): spaCy NER
    # tags "Hormuz"/"Bosphorus" as a PERSON, and while a 5M-prior region entry got vetoed and deleted, a
    # _WATERS entry sits at facility prior and is never vetoed. This is exactly the bare form the AI's
    # WHERE line emits ("WHERE: Strait of Hormuz").
    ("Strait of Hormuz", "", "Hormuz", "bare curated strait resolves; not NER-vetoed to nothing"),
    ("Bosphorus", "", "Bosphorus", "bare curated strait resolves despite NER reading it as a person"),
    ("Bab-el-Mandeb", "", "Bab el Mandeb", "hyphen folds to spaces; bare chokepoint resolves"),
    ("Taiwan Strait", "", "Taiwan Strait", "the STRAIT, not Taiwan the country/capital (regression guard)"),
    ("Barges idle as the Danube's water level keeps dropping", "", "!Danube",
     "a continental river is a LINE, not a point — a lone mention must not hijack the dot onto it"),
    ('Nikita Bier resigns as X\'s Head of Product, will continue as an "advisor."',
     'JUST IN - Nikita Bier resigns as X\'s Head of Product, will continue as an "advisor." '
     'Source: https://x.com/nikitabier/status/2085105586966827343 @disclosetv',
     "!Nikita",
     "SHIPPED BUG: dotted 'Nikita', a village near Yalta — the trailing 'in' of the promo lead 'JUST IN' "
     "read as '…IN Nikita', flipping on locating context that defeats the tiny-town + surname vetoes. A "
     "person's forename is not a town; the wire's promo lead must be stripped before geolocating."),
    ("Senate minority leader criticizes Trump's proposed 'Golden Fleet' battleships as costly vanity project",
     "", "!Golden", "SHIPPED: a program NAME ('Golden Fleet') dotted Golden, Colorado; the words of a "
     "quoted program name are modifiers, not the town (and not Fleet, UK either) — a US Senate story"),
    ("Trump unveils 'Golden Dome' missile defense shield", "", "!Golden",
     "'Golden Dome' is a program, not the city Golden (nor Dome, Ghana) — falls through to the US actor"),
    ("Russian Military Strikes Ukrainian Port with AI-Enabled Geran-4 drones over the Black Sea",
     "", "Odesa",
     "SHIPPED BUG: no port was NAMED, so the only place in the post was the BLACK SEA and the dot "
     "floated in open water — flying a TURKISH flag, the sea's nominal country. The converse of "
     "'ships cannot burn on land': A PORT CANNOT BE IN THE MIDDLE OF A SEA"),
    ("Ukrainian drones strike the port of Azov", "", "Azov",
     "GUARD: a NAMED port still wins — the unnamed-port rule must never override it"),
    ("Zelensky said Ukrainian forces struck a refinery in Bashkortostan, and the Afipsky refinery "
     "in Krasnodar region.", "", "Afipsky Refinery",
     "SHIPPED BUG (NOEL_REPORTS): the facility was DELETED — NER calls 'Afipsky refinery' an ORG and "
     "no preposition sat in front of it — so the dot fell back to Krasnodar. A curated facility is "
     "never vetoed, outranks a city that merely got the preposition, and beats 'Zelensky SAID'"),
    ("Ukrainian drones struck the Ufa refinery in Bashkortostan overnight", "", "Ufa Refinery",
     "GeoNames has no Ufa at all (1.1M) — 'a refinery in Bashkortostan' resolved to NOTHING"),
    # --- a NATIONALITY / SHIP'S FLAG is not a place, and may never be the "located" scene ---
    ("'US national' arrested on India", "", "India",
     "SHIPPED BUG: dotted the US. A man's passport is not a location"),
    ("Russia struck the Tanzania-flagged cargo vessel ATLAS BE off the coast of Odessa", "", "Odessa",
     "SHIPPED BUG: dotted TANZANIA. 'STRUCK the TANZANIA-flagged ship' — the verb points at the SHIP, "
     "and a flag of convenience is the least locational fact in existence. Russia is the ATTACKER"),
    ("All according to plan. Authorities in Russia's Bashkortostan said today's strike on the refinery",
     "", "Bashkortostan",
     "SHIPPED BUG: dotted RUSSIA. In 'in RUSSIA'S Bashkortostan' the preposition governs the phrase, "
     "whose head is Bashkortostan — a possessive may never enter the 'located' pool, where it wins "
     "outright before the sink logic is ever consulted"),
    ("US halts removal of refuelers from Ben Gurion Airport", "", "Ben Gurion",
     "SHIPPED BUG: dotted the US. A named facility beats a bare country outright"),
    ("Hungarian defense minister promises to slam the door on Ukraine", "", "Budapest",
     "SHIPPED BUG: dotted UKRAINE — 'hungarian' was one of 141 countries with NO demonym at all"),
    ("EU advances accession talks with Ukraine, Moldova, Montenegro", "", "Brussels",
     "'talks WITH Ukraine' — Ukraine is the other party, not the venue. The EU meets in Brussels"),
    ("Trump welcomes new Iraqi prime minister to White House", "", "White House",
     "SHIPPED BUG: dotted IRAQ. The White House was not in the gazetteer at ALL — a seat of power "
     "is a PLACE, not just an abstraction"),
    ("16 Indians killed or missing in Middle East since the war began", "", "India",
     "SHIPPED BUG: dotted the UNITED STATES. Only the singular 'indian' was ever a demonym, and "
     "every nationality is routinely pluralised"),
    ("Greenlandic institute not to take part in new project", "", "Greenland",
     "SHIPPED BUG: dotted the US — 'greenlandic' was not a demonym"),
    ("How jihadist groups like Boko Haram use AI for acts of terror",
     "Researchers at Cambridge University said the trend was alarming.", "Nigeria",
     "SHIPPED BUG: dotted CAMBRIDGE, UK — scraped from an academic quoted in the SUMMARY. An armed "
     "group named in the TITLE has a theatre, and the title is the story"),
    # --- a WATER named attributively is a COASTLINE, not the water ---
    ("Russia strikes Ukrainian drone industry and Black Sea ports",
     "Russian forces targeted Ukrainian drone production and storage sites in Kiev, along with port "
     "infrastructure in Odessa and Yuzhny, Moscow says", "Kiev",
     "SHIPPED BUG: the dot sat in OPEN WATER in the middle of the Black Sea while the story's own "
     "first line named the ports. Two faults: 'BLACK SEA ports' is a coastline (a port is at a quay, "
     "not at sea), and CONTAINMENT treated the sea as a 'city inside Russia' and upgraded to it. "
     "When the headline names no genuine scene, read the summary"),
    ("Burning Russian tankers in the Sea of Azov after Ukrainian drone attacks", "", "Sea of Azov",
     "GUARD: a REAL sea event must stay at sea — ships cannot burn on land"),
    ("Russian warship hit in the Black Sea", "", "Black Sea",
     "GUARD: the Black Sea itself, with no attributive noun, is still the scene"),
    # --- the AUDIT round: every one of these was a real dot in the wrong country ---
    ("U.S. strikes on Rask, Sistan and Baluchestan Province, Iran", "", "Rask",
     "SHIPPED BUG: dotted the UNITED STATES. Bare 'strikes' cannot go in _ACTOR_NOUNS (in 'Gaza "
     "strikes will continue' it is a noun) — but 'strikes ON x' can only be the attacker"),
    ("Heavy explosion in Sirik, likely US Airstrikes", "", "Sirik",
     "SHIPPED BUG: dotted the US. Sirik was not in the gazetteer, so with no scene the ATTACKER was "
     "the only hit left and won"),
    ("USAF Airstrikes against Khorram Abad, Lorestan Province", "", "Khorram Abad",
     "SHIPPED BUG: dotted the whole PROVINCE. spaCy tags 'Khorram Abad' PERSON and 'airstrikes' was "
     "not a _GEO_ACTION, so nothing marked the city as located and the NER veto deleted it"),
    ("Singaporean arrested in Bali after woman found dead in resort", "", "Indonesia",
     "SHIPPED BUG: dotted Bali, INDIA (pop 296,973) — the Indonesian island was not in the "
     "gazetteer at all"),
    ("NZ's South Island struck by magnitude-5.9 earthquake", "", "New Zealand",
     "SHIPPED BUG: fell back to the PUBLISHER (abc.net.au -> Australia); 'nz' was not an alias"),
    ("Stink bomb attack planners taken to court by energy giant Woodside", "", "Australia",
     "SHIPPED BUG: dotted Woodside, CALIFORNIA — Woodside is an Australian energy company"),
    # --- a country used ATTRIBUTIVELY is the actor ("U.S. attack") ---
    ("A U.S. attack targeted the city of Saravan in the Baluchistan province of southeastern Iran",
     "", "Saravan",
     "SHIPPED BUG: dotted the UNITED STATES. 'U.S. ATTACK' names who did it, exactly like a demonym"),
    ("Malian and Russian forces reclaim strategic northern town", "", "Mali",
     "SHIPPED BUG: dotted RUSSIA — 'malian' was not in DEMONYMS, so the only actor left standing was "
     "the Russian one. A missing demonym hands the dot to whoever else is in the sentence"),
    ("Israeli Foreign Minister Gideon Saar says his country is ready to move forward", "", "Jerusalem",
     "SHIPPED BUG: dotted ROME. A named MINISTER speaking is his ministry speaking"),
    ("Fighter jet sound over East Azerbaijan, Northeastern Iran", "", "Iran",
     "SHIPPED BUG: dotted the COUNTRY Azerbaijan — East Azerbaijan is an IRANIAN province"),
    # --- surnames and acronyms are not cities ---
    ("U.S. Trade Representative Jamieson Greer rejects EU's tech rules", "", "United States",
     "SHIPPED BUG: dotted GREER, South Carolina (pop 28k). spaCy tagged NO person at all, so the NER "
     "veto never fired — the gazetteer must guard surnames itself"),
    ("President Trump's former campaign manager Brad Parscale ran a MAGA influence operation",
     "", "United States",
     "SHIPPED BUG: 'MAGA' resolved to Maga, CAMEROON"),
    ("New York becomes first state to impose one", "", "New York",
     "SHIPPED BUG: the gazetteer had no 'new york', so it matched YORK"),
    ("Young Germans opting out of military service as Berlin strives to boost army", "", "Berlin",
     "SHIPPED BUG: dotted YOUNG, URUGUAY. A sentence-initial ordinary word is not a dateline"),
    ("Paramount's Warner takeover challenged by 12 US states", "", "United States",
     "SHIPPED BUG: dotted the TOWN of Paramount, California"),
    ("Meta used AI to target workers with medical conditions for layoffs", "", "United States",
     "SHIPPED BUG: CNA printed it, so it was dotted on SINGAPORE. A global company is headquartered "
     "somewhere, and that beats the publisher"),
    ("Man charged with murder over death in Victoria's east", "", "Victoria, Australia",
     "SHIPPED BUG: dotted VICTORIA, HONG KONG (pop 956k beat the Australian state)"),
    # --- when NOTHING is a scene, the ACTOR is the scene (a state acts at home) ---
    ("Iran's IRGC released footage of this morning's ballistic missile launches towards U.S. bases "
     "in the region.", "", "Iran",
     "SHIPPED BUG: dotted the UNITED STATES. Iran sank as an actor, and the only survivor was the "
     "country the missiles were aimed AT. An actor may only sink below a genuine SCENE"),
    ("Former Iranian President Ahmadinejad attended a ceremony commemorating Iran's late Supreme "
     "Leader today following reports claiming he was an Israeli asset over his contacts with Israel.",
     "", "Iran",
     "SHIPPED BUG: dotted ISRAEL — a country the post merely GOSSIPED about. The ceremony was in Iran"),
    ("Israeli forces raid Jenin in the West Bank", "", "Jenin",
     "GUARD: the facility upgrade must not fire when there is no facility — countries carry a "
     "HIGHER prior than facilities, and a sloppy prior test promoted ISRAELI over the real scene"),
    # --- water bodies: ships cannot burn on land ---
    ("Burning Russian tankers in the Sea of Azov after Ukrainian drone attacks", "", "Sea of Azov",
     "SHIPPED BUG: the SEA of Azov was dotted on the TOWN of Azov — ships were on land"),
    ("Ukrainian drones strike the port of Azov", "", "Azov, Russia",
     "the TOWN must still resolve when the sea is not named"),
    ("Houthis attack a cargo ship in the Red Sea", "", "Red Sea", "maritime attack"),
    ("Explosion damages the Kerch Strait bridge", "", "Kerch Strait", "strait, not a city"),
    ("Migrant boat capsizes in the English Channel", "", "English Channel", "channel"),
    ("Russian warship hit in the Black Sea", "", "Black Sea",
     "SHIPPED BUG: a demonym is the ACTOR, not the scene"),
    ("The number of people injured in Russia's attack on Zaporizhzhia has risen to nine", "", "Zaporizhzhia",
     "SHIPPED BUG: a POSSESSIVE is an actor — 'in RUSSIA'S attack on X' happens at X. Also: NER "
     "mislabelled Zaporizhzhia PERSON and deleted it; 'attack ON x' is hard locational evidence"),
    # --- a national MINISTRY acting is news at ITS OWN capital; the foreign place is the SUBJECT ---
    ("Türkiye's Foreign Ministry commemorates Srebrenica genocide", "", "Ankara",
     "SHIPPED BUG: dotted BOSNIA. The ceremony was held in Ankara — Srebrenica is what it was ABOUT. "
     "Also: 'Türkiye' tokenised to ['t','rkiye'] and the country did not exist to the geolocator"),
    ("Russia's Defense Ministry says its forces captured Toretsk", "", "Toretsk",
     "GUARD: a ministry REPORTING a real event must not drag the dot to Moscow — 'captured X' is a scene"),
    ("Israeli officials say Gaza strikes will continue", "", "Gaza",
     "GUARD: 'officials' is not a state ORGAN — this story really is about Gaza"),
    # --- apostrophe-transliterated names: the apostrophe is a TOKEN SEPARATOR ---
    ("Ansarullah authorities have announced that Sana'a International Airport has been repaired "
     "two days after the Saudi attacks", "", "Sana'a International Airport",
     "SHIPPED BUG: dotted Sana, PERU. \"Sana'a\" tokenises to sana|a; the lone 'sana' matched a "
     "Peruvian town. The airport facility (spaced key 'sana a international airport') pins it exactly"),
    ("Explosions reported near Sana'a overnight", "", "Sana'a",
     "bare Sana'a (no 'airport') must still resolve to Yemen, not Peru"),
    ("Saudi-led coalition bombs Ta'izz", "", "Ta'izz",
     "Ta'izz resolved to NOTHING before — GeoNames only had the accentless 'taiz'"),
    ("Houthis strike a tanker off Hodeidah", "", "Hodeidah",
     "'Houthis' is a Yemeni actor (Ansarullah) — it vouches Yemen as context and sinks as the actor"),
    # --- a place-name that is really part of a PROPER NOUN or a PERSON'S NAME ---
    ("House Republicans resurrect Save America Act by adding it to a spending bill", "", "United States",
     "SHIPPED BUG: dotted Save, BENIN — 'Save' is a common verb inside the bill name, not a town"),
    ("Maltese politicians involved in plot to kill Daphne Caruana Galizia, court hears", "", "Malta",
     "SHIPPED BUG: dotted Daphne, ALABAMA — 'Daphne Caruana Galizia' is the murdered journalist (a "
     "forename followed by a capitalised surname, after the naming-verb 'kill')"),
    ("Lebanon talks in Rome wrap up as US and Israeli officials describe them as positive", "", "Rome, Italy",
     "SHIPPED BUG: dotted Rome, GEORGIA (pop 36k). 'US officials' put the US in context and demoted "
     "the world capital — a >=20x population gap must let the dominant city win"),
    # --- a person's NATIONALITY is not a location, even when it is the ONLY geo token in the title ---
    ("Venezuelan man becomes 22nd person to die in ICE custody this year",
     "Jesus Manuel Arenas-Silva, 45, found unresponsive while being transferred between detention "
     "facilities in Georgia. Another person has died in federal immigration custody this week in Georgia.",
     "Georgia",
     "SHIPPED BUG: dotted CARACAS. 'Venezuelan man' is a passport, not the scene. With no place in the "
     "title, the SUMMARY's real scene (Georgia) must win — not the nationality"),
    ("Two Iranian nationals detained in Germany over alleged bomb plot", "", "Germany",
     "the nationality ('Iranian nationals') sinks; Germany is where it happened"),
    ("Ukrainian forces recapture a village near Kupiansk", "", "Kupiansk",
     "GUARD: 'forces' is a STATE ACTOR (not a _PERSON_NOUN), so 'Ukrainian' must NOT be dropped as a "
     "mere nationality — the country stays a party and the scene resolves normally"),
    ("Russia expands anti-drone defenses over nuclear submarines at the Rybachiy base on Kamchatka",
     "More than five submarines are visible beneath large protective nets, compared with only two in May.",
     "!India", "SHIPPED: 'only two in May' dotted the town of May, India — a month is a DATE, never a place"),
    ("JRS launches project to strengthen oil spill resilience in Akwa Ibom community",
     "A community in Nigeria is getting help to deal with oil spills. The initiative is backed by CARITAS Canada.",
     "Nigeria", "SHIPPED: dotted CANADA off 'CARITAS Canada' — an aid group's home country is not the scene; "
     "the located 'in Nigeria' must win (the AI WHERE prompt now says the same for the summary-pass pinpoint)"),
]

# geolocation cases that also need the article URL (the section is a country hint)
GEO_URL_CASES = [
    ("Georgia teen in plea hearing over school shooting", "Apalachee high school in Georgia.",
     "https://www.theguardian.com/us-news/2026/jul/12/georgia", "Georgia, United States",
     "SHIPPED BUG: the US STATE was read as the COUNTRY in the Caucasus"),
    ("Georgia and Russia trade accusations over the border", "Tbilisi summoned the ambassador.", "",
     "Georgia", "the same name must still resolve to the COUNTRY when the story is about it"),
    ("African growth boom follows Trump push to replace aid with trade",
     "Across Asia and Africa, growth is accelerating.", "", "!Philippines",
     "SHIPPED BUG: dotted Asia, PHILIPPINES — a real town of 23,546. A CONTINENT names a whole "
     "hemisphere and can never be the scene of a single event"),
    # The SECTION beats the PUBLISHER. These all fell back to the outlet's home country.
    ("Hunter Biden says rule of law prevailed in defamation lawsuit", "",
     "https://www.theguardian.com/us-news/2026/jul/14/hunter-biden-defamation", "United States",
     "SHIPPED BUG: dotted IRAN. The URL section (/us-news/) is the DESK that filed it — far better "
     "evidence than where the outlet's office happens to be"),
    ("Albanese to compare pivotal moment in AI to renewable energy transition", "",
     "https://www.theguardian.com/australia-news/2026/jul/14/albanese-ai-speech", "Australia",
     "SHIPPED BUG: dotted the UK because the Guardian is British"),
    ("Sorry, conspiracy theorists, Lindsey Graham isn't worth your effort", "",
     "https://www.rt.com/news/643036-lindsey-graham-conspiracy-theories/", "United States",
     "SHIPPED BUG: dotted RUSSIA because RT printed it. A US senator's story is US news whoever "
     "prints it — the SUBJECT's country beats the publisher's"),
    ("Shanmugam and Tan See Leng to donate Bloomberg damages to charity", "",
     "https://www.channelnewsasia.com/singapore/shanmugam-tan-see-leng-bloomberg", "Singapore",
     "REGRESSION GUARD: the fallback LADDER ORDER is itself a bug surface. Putting the org check "
     "above the URL section sent this Singapore court story back to the US on the word 'Bloomberg'. "
     "Order must be: URL section -> org in title -> summary -> subject -> publisher (last)"),
]

# headlines: never cut mid-word, always start with a capital. (raw_post, check_fn_name, why)
HEADLINE_CASES = [
    ("Beneath Helsinki lies a vast underground network of around 5,500 shelters capable of protecting nearly one million people in the event of a nuclear strike or Russian attack, The Times reports.",
     "no_midword", "SHIPPED BUG: headline ended '...or Russian attack, The Ti'"),
    ("imagery also shows significant damage to the AVT-6 crude distillation unit at the Syzran oil refinery. Fire engines were visible operating around both damaged units.",
     "capitalised", "SHIPPED BUG: a Telegram continuation post began lower-case"),
    ("Russian Foreign Minister Sergey Lavrov:\n\nEurope and Ukraine buried the agreements reached in "
     "Alaska between Russia and the US.",
     "has_content", "SHIPPED BUG: the headline was 'Russian Foreign Minister Sergey Lavrov:' — a "
     "LEAD-IN with no news in it. These channels put the speaker on line 1 and what he SAID on line 2"),
    ('Russian channels are reporting an explosion at the "Balzi Rossi" restaurant on Kudrinskaya Street '
     'in. Moscow, near the US embassy.',
     "no_dangle", "SHIPPED BUG: a stray period after 'in' (a translation artifact) cut the headline to a "
     "dangling '...Street in.' — ends on punctuation yet reads mid-thought. Drop the dot so it reads on"),
    ("Ukrainian forces launched a large-scale overnight assault on the port city and struck several targets in.",
     "no_dangle", "a source truncated mid-phrase ('...targets in.'); drop the dangling preposition"),
]

# the wire dateline is the canonical event location. (headline, summary, expected, why)
DATELINE_CASES = [
    ("Two women hurt in Ukraine's attack on passenger bus in LPR",
     "LUGANSK, July 12. /TASS/. Two women were injured in a Ukrainian attack on an intercity bus in the Lugansk People's Republic (LPR).",
     "Lugansk", "SHIPPED BUG: dotted on Ukraine's capital; the wire dateline says LUGANSK"),
    ("Israel steps up raids",
     "BEIRUT (Reuters) - Israeli jets struck Beirut's southern suburbs overnight, Lebanese officials said.",
     "Beirut", "the headline names only the ACTOR; the dateline names the scene"),
    ("Russia says it will respond to new sanctions",
     "LONDON (Reuters) - Russia said on Sunday it would respond.",
     "Russia", "a BUREAU dateline must NOT win — this story is not about London"),
    ("Ukrainian drones are active over occupied Crimea, with a group seen flying above the Sovetsky district.",
     "", "Sovetsky", "CONTAINMENT: a town named inside a broad area wins"),
]

# A brief should just START — a wire dateline / place-stamp ("TEHRAN — ", "BEIRUT, Lebanon (Reuters) — ")
# is stripped from the lead, but only when it is an ALL-CAPS place + spaced dash, so a Title-cased sentence
# or a hyphenated compound survives untouched. (raw, expected_after__strip_promo, why)
DATELINE_STRIP_CASES = [
    ("TEHRAN – Iran said it would resume enrichment.", "Iran said it would resume enrichment.",
     "SHIPPED: the lead opened with 'TEHRAN –', which looks bad on the card"),
    ("WASHINGTON — The White House announced new sanctions.", "The White House announced new sanctions.",
     "em-dash place-stamp"),
    ("BEIRUT, Lebanon — Hezbollah fired rockets across the border.", "Hezbollah fired rockets across the border.",
     "place + ', Country' + em-dash"),
    ("NEW DELHI (Reuters) — India tested a new missile.", "India tested a new missile.",
     "two-word place + '(Agency)' + em-dash"),
    ("KYIV, Ukraine (Reuters) - Ukraine struck a Russian depot.", "Ukraine struck a Russian depot.",
     "place + country + agency + hyphen"),
    # MUST SURVIVE — not a dateline:
    ("Trump — the president — said he would act.", "Trump — the president — said he would act.",
     "a Title-cased sentence with an em-dash aside is NOT a dateline"),
    ("TEHRAN-based militias regrouped overnight.", "TEHRAN-based militias regrouped overnight.",
     "a hyphenated compound (no space after the dash) is not a dateline"),
    ("NATO said the alliance would respond.", "NATO said the alliance would respond.",
     "an ALL-CAPS acronym with no dash is left alone"),
]

# clips must only attach to the story they belong to. (event, clip, should_attach, why)
CLIP_CASES = [
    ("The JNIM has also attacked VDP outposts in Burkina Faso this weekend, seizing control of one in Konkoura and pillaging two others in northern Burkina Faso.",
     "At least 27 people were killed after a massive fire engulfed a pub in northern Bangkok shortly after midnight on Monday.",
     False, "SHIPPED BUG: attached on {'control','northern'} — 'seizing CONTROL...NORTHERN Burkina Faso' vs 'brought under CONTROL...NORTHERN Bangkok'. Pure coincidence."),
    ("Trump slams California plan to raise the minimum wage for fast-food workers",
     "BREAKING - Trump says Israel 'very happy' about Hamas disarmament deal",
     False, "SHIPPED BUG: a ubiquitous shared name (Trump) + same country (US) pulled an unrelated Hamas clip onto a California min-wage dot. A shared name needs a shared TOPIC, not just the same country."),
    ("Supermarket in Zaporozhye Region attacked by Ukrainian drone",
     "FP-1 strike drones maneuvering before hitting the Syzran oil refinery in Russia's Samara region",
     False, "SHIPPED BUG: attached on {attack, drone, region} — conflict filler"),
    ("Supermarket in Zaporozhye Region attacked by Ukrainian drone",
     "Burning Russian tankers in the Sea of Azov after Ukrainian drone attacks", False,
     "SHIPPED BUG: 'Ukrainian' is capitalised but identifies nothing — half the war shares it"),
    ("What made US Republican Senator Lindsey Graham a lightning rod",
     "President Trump on Lindsey Graham: I'm a big Israel supporter", True,
     "a person-led story may cross borders and must still attach"),
    ("Supermarket in Zaporozhye Region attacked by Ukrainian drone",
     "Aftermath of the drone strike on the supermarket in Zaporozhye Region", True,
     "the same event must attach"),
    ("Bangkok pub fire kills at least 27 people",
     "At least 27 people were killed after a massive fire engulfed a pub in northern Bangkok.", True,
     "the clip belongs to ITS OWN story"),
    # A MENTION IS NOT A SUBJECT. Match the clip's first sentence — what the post is ABOUT.
    ("Kyiv claims drone attacks on 11 Russian tankers in Sea of Azov",
     "Russian Foreign Minister Sergey Lavrov claimed Europe and Ukraine buried US-Russia agreements "
     "reached in Alaska and tried to sideline Washington. He also labeled Ukrainian drone strikes on "
     "Russian vessels in the Sea of Azov “terrorism.”", False,
     "SHIPPED BUG: a LAVROV TALKING-HEAD about the peace plan was filed under a tanker strike, because "
     "its SECOND sentence tacked on 'he also called the Azov strikes terrorism'. Match the SUBJECT"),
    ("Kyiv claims drone attacks on 11 Russian tankers in Sea of Azov",
     "Zelensky said Ukrainian forces struck a refinery in Bashkortostan and the Afipsky refinery in "
     "Krasnodar region. He also confirmed hits on 3 tankers in the Sea of Azov.", False,
     "SHIPPED BUG: a REFINERY clip attached to the tanker story on the same trailing mention"),
    ("Kyiv claims drone attacks on 11 Russian tankers in Sea of Azov",
     "Burning Russian tankers in the Sea of Azov after Ukrainian drone attacks", True,
     "the ACTUAL footage of this event must still attach"),
    # A SHARED LOCATION IS NOT A SHARED SUBJECT. A ship struck OFF Odesa and a street protest IN Odesa
    # are both in Ukraine and both say "Odesa" — but the protest is not footage of the ship attack.
    ("Russian attack on corn ship off Ukraine's Odesa kills 10",
     "Protests calling for the reinstatement of Mykhailo Fedorov and the resignation of "
     "Commander-in-Chief Oleksandr Syrskyi continue in Lviv, Odesa, Ternopil and other cities.", False,
     "SHIPPED BUG: an Odesa PROTEST attached to an Odesa SHIP strike on the shared place-name alone"),
    ("Russian attack on corn ship off Ukraine's Odesa kills 10",
     "Footage of the burning cargo ship off Odesa after the Russian missile strike.", True,
     "the ACTUAL ship footage shares 'ship' beyond the place and must still attach"),
]

# Two DIFFERENT events must not be merged. (headline_a, headline_b, same_event?, why)
DEDUP_CASES = [
    ("Satellite imagery confirms fuel storage tanks burned at the Tvernefteprodukt oil depot in Tver after the overnight strike",
     "Russian drones struck an oil refinery in Omsk overnight", False,
     "SHIPPED BUG: every Russia+security story shares {drone,strike,oil,refinery}, so a real strike on Tver was deleted as a 'duplicate' of Omsk"),
    ("Ukrainian drone strike hits the Omsk oil refinery",
     "Drone strike sets Omsk oil refinery ablaze, officials say", True,
     "the same event reported twice SHOULD still merge"),
]

# SIMILARITY METER (_same_story): the SAME story from two sources/channels — even when one copy carries an
# extra prefix, is re-headlined, or is classified differently — must be seen as one. Two genuinely
# different events (different city, different numbers) must NOT. (title_a, title_b, want_duplicate, why)
SIM_CASES = [
    ("President Trump via Truth Social: Afghanistan War: 20 years, 2,000 DEAD.",
     "Afghanistan War: 20 years, 2,000 DEAD.", True,
     "SHIPPED BUG: the same Truth Social post from two channels stacked as two Afghanistan dots — the "
     "'President Trump via Truth Social:' prefix diluted the distinctive-word overlap to {afghanistan,year} "
     "and pushed one copy into a different category, so every existing dedup gate missed it"),
    ("Zelensky addresses the nation on the Pokrovsk front",
     "BREAKING: Zelensky addresses the nation on the Pokrovsk front, via his evening address", True,
     "a re-headline with extra framing words is still the same address"),
    ("Satellite imagery confirms fuel storage tanks burned at the Tvernefteprodukt oil depot in Tver after the overnight strike",
     "Russian drones struck an oil refinery in Omsk overnight", False,
     "GUARD: different cities (Tver vs Omsk) must NOT merge just for sharing 'oil' and 'overnight'"),
    ("Russian missile strike on Kharkiv kills three",
     "Russian drone strike on Kyiv kills five", False,
     "GUARD: two different strikes on two different cities are two different events"),
]

# STARRED COUNTRIES — country_news() maps a country to GDELT's FIPS code (NOT ISO) and only keeps stories
# that land in that country. (country_name, expected_fips)
FIPS_CASES = [
    ("Latvia", "LG"), ("United States of America", "US"), ("Germany", "GM"),
    ("South Korea", "KS"), ("United Kingdom", "UK"), ("Russia", "RS"), ("Czechia", "EZ"),
    ("Narnia", ""),   # unknown country -> not starrable, returns "unsupported" not a crash
]
# geolocated country vs the starred name must agree across naming variants. (a, b, want_match)
CMATCH_CASES = [
    ("United States of America", "United States", True),
    ("Czechia", "Czech Republic", True),
    ("Britain", "United Kingdom", True),
    ("Latvia", "Germany", False),
]

# Auto-update triggers only when the release is genuinely newer — compared numerically, not as strings
# ("1.10.0" must beat "1.2.0"). (remote_tag, local_version, expect_is_newer)
VER_CASES = [
    ("1.0.1", "1.0.0", True), ("v1.1.0", "1.0.9", True), ("2.0.0", "1.9.9", True),
    ("1.0.0", "1.0.0", False), ("1.0.0", "1.0.1", False), ("v1.2", "1.10.0", False),
]

# CURRENT LEADERS — a shared surname is NOT the same person (Ali vs his son Mojtaba Khamenei). (a, b, match)
NAMEMATCH_CASES = [
    ("Mojtaba Khamenei", "Ali Khamenei", False),
    ("Ali Khamenei", "Ali Hoseini-Khamenei", True),
    ("Donald Trump", "Donald John Trump", True),
    ("Emmanuel Macron", "Macron", True),
    ("Keir Starmer", "Rishi Sunak", False),
]

import datetime as _dt
_RECENT = (_dt.datetime.utcnow() - _dt.timedelta(days=20)).strftime("%Y-%m-%d")
# _pick_leader: pick the CURRENT officeholder, cross-checked against the Factbook name.
# (candidates, factbook_name, expected_name, why)
LEADER_PICK_CASES = [
    # The head of state's term is ENDED on Wikidata (died / replaced) but a stale Factbook still lists him.
    # Show the LIVE successor — never resurrect the former/dead leader.
    ([{"qid": "Q1", "name": "Ali Khamenei", "ended": True, "start": "1989-06-04", "preferred": False},
      {"qid": "Q2", "name": "Mojtaba Khamenei", "ended": False, "start": "", "preferred": True}],
     "Ali Khamenei", "Mojtaba Khamenei",
     "SHIPPED BUG: a stale Factbook resurrected a leader whose Wikidata term had ENDED — never show a former leader"),
    # a leader with a DATE OF DEATH is never current, even if the term claim wasn't flagged ended
    ([{"qid": "Q1", "name": "Dead Leader", "ended": False, "dead": True, "start": "2010-01-01", "preferred": True},
      {"qid": "Q2", "name": "Live Successor", "ended": False, "dead": False, "start": "2026-02-15", "preferred": False}],
     "Dead Leader", "Live Successor",
     "a leader who has DIED (P570) can never be current, whatever a lagging source says"),
    ([{"qid": "Q1", "name": "Donald Trump", "ended": False, "start": "2025-01-20", "preferred": True}],
     "Donald Trump", "Donald Trump", "the sole current holder is picked"),
    ([{"qid": "Q1", "name": "Old Leader", "ended": True, "start": "2019-01-01", "preferred": False},
      {"qid": "Q2", "name": "New Leader", "ended": False, "start": _RECENT, "preferred": True}],
     "Old Leader", "New Leader",
     "the live successor beats a lagging Factbook name"),
    ([{"qid": "Q1", "name": "A Person", "ended": False, "start": "2020-01-01", "preferred": True}],
     "", "A Person", "no cross-check source -> take the live/preferred pick"),
    ([], "Someone", None, "no candidates -> None"),
]

# Parsing the Factbook's free-text leader field (used to fill roles Wikidata lacks, e.g. Saudi's PM).
# (raw, expected_name_startswith, expected_title)
FB_PARSE_CASES = [
    ("Crown Prince and Prime Minister MUHAMMAD BIN SALMAN bin Abd al-Aziz Al Saud (since 27 September 2022)",
     "Muhammad bin Salman", "Crown Prince and Prime Minister"),
    ("President Donald J. TRUMP (since 20 January 2025)", "Donald", "President"),
    ("King SALMAN bin Abd al-Aziz Al Saud (since 23 January 2015)", "Salman", "King"),
    ("Prime Minister Keir STARMER (since 5 July 2024)", "Keir Starmer", "Prime Minister"),
]

# _same_person: two resolved leaders are the same individual only if QIDs match, OR (Factbook-only, no QID)
# they share a given name AND family name. A shared surname alone must NOT merge two people.
# (name_a, qid_a, name_b, qid_b, expected_same)
SAME_PERSON_CASES = [
    # Saudi: King Salman vs his son Mohammed bin Salman — share 'bin Salman al Saud' but are DIFFERENT people.
    ("Salman bin Abd al-Aziz Al Saud", None, "Muhammad bin Salman Al Saud", None, False),
    # the same person named identically in both Factbook fields must collapse to one
    ("Emmanuel Macron", None, "Emmanuel Macron", None, True),
    # QIDs are authoritative: same QID = same person, different QID = different people
    ("King Salman", "Q1", "Salman", "Q1", True),
    ("Person A", "Q1", "Person B", "Q2", False),
    # transliteration of the same given+family name still matches (Factbook-only)
    ("Recep Tayyip Erdogan", None, "Recep Erdogan", None, True),
]

# A leader Wikidata says has DIED (P570) must never be resurrected by the lagging CIA Factbook. We remember
# the dead and refuse a Factbook name that matches one. (dead_name, factbook_name, expected_is_dead, why)
DEAD_LEADER_CASES = [
    ("Ali Khamenei", "Ali Hoseini-Khamenei", True, "SHIPPED BUG: a rate-limited fetch showed the late Ali Khamenei because the Factbook still lists him"),
    ("Ali Khamenei", "Masoud Pezeshkian", False, "a living leader must never be blocked as dead"),
    ("Ali Khamenei", "Mojtaba Khamenei", False, "the living successor shares only the surname — he is not the dead man"),
    ("Ebrahim Raisi", "Ebrahim Raisi", True, "an exact dead name is blocked"),
    ("Abdullah bin Abdulaziz Al Saud", "Salman bin Abdulaziz Al Saud", False,
     "a dead former king must NOT mark the living king dead — they only share the family name"),
]

# vxTwitter/fixvx Telegram reposts: the reposter's throwaway comment must not become the headline, and the
# emoji reaction-counts must be stripped. (raw_post, headline_must_contain, why)
TG_CLEAN_CASES = [
    ("This is a war crime dawg\nhttps://x.com/j/status/1\nvxTwitter / fixvx \U0001f48b 88 \U0001f4e9 36\n"
     "Jungle Journey ()\nUkraine strikes a gas station in Russia's Belgorod region as civilians wait to fill up "
     "\U0001f48b40 \U0001f47a35 \U0001f92311",
     "Ukraine strikes a gas station",
     "SHIPPED BUG: 'This is a war crime dawg' (the reposter's comment) became the headline over the tweet"),
    ("Explosions reported in Kharkiv overnight, local officials say\nMore: https://x.com/s/status/9",
     "Explosions reported in Kharkiv",
     "a normal post that merely links to X must keep its own text (no over-stripping)"),
]

# Governing-lean meter: map a party's documented political alignment to -3..+3 (compound terms like
# 'centre-right' must beat the bare 'right-wing'). (alignment_labels, expected_score)
LEAN_CASES = [
    (["right-wing politics"], 2), (["left-wing politics"], -2), (["far-right politics"], 3),
    (["centre-left politics"], -1), (["centre-right politics"], 1), (["centrism"], 0),
    (["big tent"], 0), ([], None), (["centre-left", "left-wing"], -2),
]

# (headline, url, should_be_dropped, why)
FLUFF_CASES = [
    ("New Scholarships. New Programs. Your Next Step.", "https://toi.li/5Jd4WB", True,
     "SHIPPED BUG: sponsored content sat on the map as an Israel dot — an advert has no event"),
    ("From Sudan to Spain: Between war and home", "https://t.me/x/1", True,
     "a personal-journey human-interest FEATURE, not a located event"),
    ("One refugee's journey from Kabul to a new life in Berlin", "/news/x", True, "profile feature, not an event"),
    ("Migrants cross the Ceuta border as Spain deploys troops", "/news/x", False,
     "a real migration EVENT stays — only the 'read my story' FEATURE form is fluff"),
    ("Germany: Islamism and right-wing extremism", "https://t.me/x/1", True,
     "a 'Country: ideology-theme' analysis headline (no event, no number) is an op-ed shape, not a dot"),
    ("USA: The roots of woke capitalism", "/news/x", True, "symmetric: an ideology think-piece of any slant is fluff"),
    ("Germany: Far-right AfD wins state election", "/news/x", False,
     "a real far-right political EVENT is NOT filtered — the think-piece filter keys on the analysis SHAPE, not the viewpoint"),
    ("Sweden: Far-right party surges in new poll", "/news/x", False, "a real poll/event about the far right stays"),
    ("United States at 250: Seven tests of the American experiment",
     "/video/featured-documentaries/x", True, "a documentary is not an event"),
    ("Republican Lindsey Graham dies at 71: World leaders react", "/news/x", False,
     "SHIPPED BUG: the 'at 250:' rule matched his AGE and dropped a major story"),
    ("US: Reporters subpoenaed over Air Force One stories", "/news/x", False,
     "SHIPPED BUG: the listicle rule matched 'One stories'"),
    ("Three Iranian ballistic missiles impacted Shuwaikh Port", "https://t.me/rnintel/1", False,
     "SHIPPED BUG: requiring an 'event verb' dropped this ('impacted' was not on the list)"),
    ("Moldova's president nominates Vasile Tofan as prime minister", "/news/x", False,
     "SHIPPED BUG: 'nominates' was not an 'event verb' either"),
    ("The week in pictures: Le Pen's comeback", "/news/x", True, "photo gallery"),
    ("Commentary: Should Western countries embrace air conditioning", "/news/x", True,
     "SHIPPED BUG: 'comment' did not match 'Commentary'"),
    ("Australia news live: auction clearances nudge up", "/news/x", True, "a live blog is not an event"),
    ("D-topia review – cosy sci-fi mystery takes aim at AI",
     "https://www.theguardian.com/games/2026/jul/14/d-topia-review-sci-fi-ai-puzzle-game", True,
     "SHIPPED BUG: a VIDEO GAME REVIEW was a dot on the map — /games/ was not a fluff path"),
]


# The FLAGS on the card = who is a PARTY to the event. A person's nationality is not.
# (headline, event_country, expected_flags, why)
FLAG_CASES = [
    ("ICE fatally shoots 26-year-old Colombian man in Maine during immigration operation",
     "United States of America", ["United States of America"],
     "SHIPPED BUG: flew the COLOMBIAN flag over a US story because the victim was Colombian. "
     "Colombia is not a party to an ICE shooting in Maine"),
    ("Israeli soldier killed in Gaza clash", "Palestine", ["Palestine", "Israel"],
     "GUARD: a SOLDIER is a state actor — his country IS a party. Only private individuals "
     "(man/woman/migrant/victim) carry a nationality that means nothing"),
    ("Ukrainian drones strike the port of Azov", "Russia", ["Russia", "Ukraine"],
     "both belligerents are parties, and the country it HAPPENED in leads"),
    ("Türkiye's Foreign Ministry commemorates Srebrenica genocide", "Turkey", ["Turkey"],
     "SHIPPED BUG: 'Türkiye' folded to nothing, so the story had NO flag at all"),
]


# Telegram writes background-image:url('…') with PLAIN quotes. The scraper only accepted the
# HTML-ESCAPED &#39; form, so it found NO photos on a normal post — and an ALBUM (a NOELREPORTS post
# with four pictures of a struck logistics hub) was scraped as "no media" and the card fell back to a
# stock photo of the city. We had the pictures all along. (raw_css_url, expected)
CSS_URL_CASES = [
    ("'https://cdn4.telesco.pe/file/abc123'", "https://cdn4.telesco.pe/file/abc123"),
    ("&#39;https://cdn4.telesco.pe/file/abc123&#39;", "https://cdn4.telesco.pe/file/abc123"),
    ("&quot;https://cdn4.telesco.pe/file/abc123&quot;", "https://cdn4.telesco.pe/file/abc123"),
    ("https://cdn4.telesco.pe/file/abc123", "https://cdn4.telesco.pe/file/abc123"),
]


# A story must never show the SAME media file twice. The twin substitution (a blocked clip served
# from a channel whose copy Telegram WILL release) ate its own tail: that playable post also matched
# the story on its own, so the identical video went out twice, side by side.
# (list_of_media_urls, expected_unique_count, why)
MEDIA_DEDUP_CASES = [
    (["https://cdn4.telesco.pe/file/A.mp4", "https://cdn4.telesco.pe/file/A.mp4"], 1,
     "SHIPPED BUG: the twin substitution served the same file as itself"),
    (["https://cdn4.telesco.pe/file/A.mp4", "https://cdn4.telesco.pe/file/B.mp4"], 2,
     "two genuinely different clips must BOTH survive"),
    (["https://cdn4.telesco.pe/file/A.jpg", "https://cdn4.telesco.pe/file/A.jpg",
      "https://cdn4.telesco.pe/file/B.jpg"], 2,
     "an album's distinct frames survive; a repeated frame does not"),
]


# _collapse_colocated merges dots on the SAME specific place within a few hours, keeping the severest.
# Each event is (title, cat, place, country, hrs). (events, expected_kept_count, kept_cat_or_None, why)
def _ev(title, cat, place, country, hrs, image=""):
    return {"title": title, "cat": cat, "place": place, "country": country,
            "hrs": hrs, "image": image, "lat": 46.5, "lng": 30.7}


# A clip belongs to ONE dot. _media_id must ignore the volatile ?token=; _assign_clips gives each
# clip to its single best-matching story. (checks run inline in main)
def _clip_assignment_ok():
    ok = True
    # the token must not change a clip's identity
    a = "https://cdn4.telesco.pe/file/abc123.mp4?token=AAA"
    b = "https://cdn4.telesco.pe/file/abc123.mp4?token=BBB"
    ok = ok and (app._media_id(a) == app._media_id(b))
    # a Sana'a-airport clip should be owned by the airport story, not a generic Yemen one
    events = [_ev("Sana'a International Airport reopens after Saudi strikes", "security",
                  "Sana'a International Airport, Yemen", "Yemen", 1.0),
              _ev("Yemen war: diplomacy stalls in Riyadh", "politics", "Riyadh, Saudi Arabia",
                  "Saudi Arabia", 2.0)]
    posts = [{"video": "https://cdn4.telesco.pe/file/clipX.mp4?token=Z",
              "text": "Footage of Sana'a International Airport reopening after the Saudi strikes."}]
    app._assign_clips(events, posts)
    owner = app._CLIP_OWNER.get(app._media_id(posts[0]["video"]))
    ok = ok and (owner == events[0]["title"])
    return ok


COLLAPSE_CASES = [
    ([_ev("Kh-22/32 impacts in Odesa", "security", "Odesa, Ukraine", "Ukraine", 0.5),
      _ev("2 on course for Odesa/Chornomorsk", "politics", "Odesa, Ukraine", "Ukraine", 0.6),
      _ev("Explosions in Odesa Port", "security", "Odesa, Ukraine", "Ukraine", 0.7)], 1, "security",
     "SHIPPED BUG: one barrage arrived as 3 terse posts split across categories and STACKED as 3 dots"),
    ([_ev("Russia strike A", "security", "Russia", "Russia", 1.0),
      _ev("Russia strike B", "security", "Russia", "Russia", 2.0)], 2, None,
     "GUARD: a bare COUNTRY is not a specific place — two Russia stories are different events"),
    ([_ev("Gaza strike at dawn", "security", "Gaza, Palestine", "Palestine", 1.0),
      _ev("Gaza aid talks at night", "politics", "Gaza, Palestine", "Palestine", 10.0)], 2, None,
     "GUARD: same place but 9h apart — different incidents, both kept"),
    ([_ev("Kyiv aid deal signed", "politics", "Kyiv, Ukraine", "Ukraine", 1.0),
      _ev("Kyiv hit by missile strike", "security", "Kyiv, Ukraine", "Ukraine", 2.0)], 1, "security",
     "co-located within the window collapses to the SEVEREST category (a strike beats an aid story)"),
]

# the Live Wire must drop an admin's PERSONAL messages (greetings, sign-offs) but keep real news,
# even speculative firehose news. (text, should_drop, why)
CHATTER_CASES = [
    ("Good night, sleep well and see you all tomorrow!", True,
     "SHIPPED BUG: an admin sign-off showed on the wire as if it were a news post"),
    ("Thanks for following today, back tomorrow morning", True, "a thank-you + sign-off"),
    ("That's all for today, stay safe everyone", True, "a wrap-up greeting"),
    ("Subscribe to our backup channel for more", True, "channel self-promotion"),
    ("Iran warns Strait of Hormuz will remain closed until US accepts its terms", False,
     "GUARD: a speculative/threat post is still NEWS — the wire is the firehose, keep it"),
    ("Israel says it will respond to the attack tomorrow", False,
     "GUARD: 'tomorrow' inside a real report is not a sign-off"),
    ("Air defense activity reported over Kuwait, residents urged to stay safe indoors", False,
     "GUARD: 'stay safe' in a warning is news, not 'stay safe everyone'"),
    ("Good Friday services held across Rome amid tight security", False,
     "GUARD: 'Good Friday' is a proper noun, not a greeting"),
]

# _tg_reliable gates what becomes a MAP DOT. An ATTRIBUTED official statement is news even when it's
# future-tense/threat wording; unverified rumour and UNattributed speculation are not. (text, keep?, why)
RELIABLE_CASES = [
    ("Iran warns Strait of Hormuz will remain closed until US accepts its terms", True,
     "an official warning is an on-record statement ('warns'), not speculation — the user was missing these"),
    ("Israel says it will respond to the attack tomorrow", True, "'says' = attributed statement"),
    ("Russian Foreign Minister Sergey Lavrov: Europe buried the agreements reached in Alaska", True,
     "a 'Speaker: …' label is an on-record statement"),
    ("Kremlin vows to retaliate against the new EU sanctions package", True, "'vows' = attributed statement"),
    ("Zelensky says Ukraine will not cede any territory in talks", True, "'says' = attributed statement"),
    ("Russia reportedly moving troops toward the Sumy border overnight", False, "'reportedly' = unverified rumour"),
    ("Unconfirmed reports of a large explosion near the airbase", False, "'unconfirmed' = rumour"),
    ("Attack on the base could be imminent, situation developing fast", False,
     "'could'/'imminent' with NO speaker = bare speculation"),
    ("Missiles might strike Tel Aviv within the hour as tensions rise", False, "unattributed speculation"),
]

# The IMPORTANCE GATE hides 'local' (AI-scoped) stories from the world map, but _hard_news is the safety
# net: a mass-casualty event or a top-official statement must NEVER be hidden even if the AI mis-scored it.
# (title, is_hard_news, why)
HARD_NEWS_CASES = [
    ("Gunman kills 9 at a shopping mall in Texas", True, "casualties always reach the map"),
    ("Air strike leaves 12 dead and 30 injured in Gaza", True, "killed + injured toll"),
    ("Iran's foreign minister warns of retaliation against Israel", True, "a top official on the record"),
    ("Kremlin says it will respond to the new sanctions", True, "an institution's statement"),
    ("EU announces fresh sanctions package on Russia", True, "institution + 'sanction' + a saying verb"),
    ("Illegal sand extraction erodes Cape Verde coastline", False, "minor/local -> defer to the AI SCOPE"),
    ("Local bakery in Lisbon revives a traditional recipe", False, "human-interest -> defer to the AI SCOPE"),
]

# terse OSINT strike posts must classify as security (so they merge), without false positives.
CLASSIFY_STRIKE_CASES = [
    ("Kh-22/32 impacts in Odesa.", "security", "missile designation is an unambiguous strong signal"),
    ("Shahed drones over Kharkiv", "security", "Shahed = attack drone"),
    ("Geran-2 spotted heading toward Sumy", "security", "Geran loitering munition"),
    ("Ukraine on course for EU membership talks", "politics",
     "GUARD: 'on course for' is NOT a strike — it was wrongly added then removed"),
    ("Economy on course for a soft landing", "economy",
     "GUARD: an economics idiom must never read as a missile"),
]


# _clean_headline strips a trailing " - Outlet". It must NOT chop a compound word or a range.
# (raw_rss_title, must_be_preserved_substring, why)
CLEAN_HEADLINE_CASES = [
    ("3 senior Iraqi Defense Ministry officers detained in anti-corruption campaign",
     "anti-corruption campaign",
     "SHIPPED BUG: `\\s*` made the separator's space optional, so the hyphen in 'anti-corruption' "
     "counted as ' - Outlet' and the headline was cut to '...detained in anti'"),
    ("Trump signs sweeping pro-democracy legislation after a long congressional fight",
     "pro-democracy legislation", "a compound with 'pro-' must survive"),
    ("Health officials track a new COVID-19 subvariant spreading across the region",
     "COVID-19 subvariant", "COVID-19 is not an outlet suffix"),
    ("Russia and Ukraine clash over the 2014-2015 Minsk agreements once again",
     "2014-2015 Minsk", "a year RANGE has no space before the dash"),
    ("Sudan faces a worsening humanitarian crisis as fighting spreads - Reuters",
     "!Reuters", "a GENUINE ' - Outlet' suffix (space before the dash) must still be stripped"),
    # WIRE-TWEET PROMO must never reach a headline or a story body (Insider Paper et al.).
    ("BREAKING - India says four nationals killed in attack on ship in Ukraine READ: https://t.co/fopymj0M2y Follow @InsiderPaper for more news",
     "India says four nationals killed in attack on ship in Ukraine",
     "the real headline must survive after the BREAKING label, link, READ:, and Follow tail are stripped"),
    ("India says four nationals killed in attack on ship in Ukraine READ: https://t.co/fopymj0M2y Follow @InsiderPaper for more news",
     "!http", "a bare link must never reach a headline"),
    ("India says four nationals killed in attack on ship in Ukraine Follow @InsiderPaper for more news",
     "!Follow", "a 'Follow @handle for more news' promo tail must be stripped"),
    ("Follow the money: how sanctioned oligarchs moved billions offshore",
     "Follow the money", "a legitimate 'Follow the …' headline must NOT be stripped as promo"),
]


# Wire copy opens with filing metadata and attributive padding, not with the news. _sharpen strips it
# so the fact lands in the first words. (raw, expected_start_or_!banned, why)
SHARPEN_CASES = [
    ("MELITOPOL, July 16. /TASS/. Two civilians of the Donetsk People's Republic were killed.",
     "Two civilians", "SHIPPED: the card opened with the filing dateline, not the news"),
    ("BEIRUT (Reuters) - Israeli jets struck Beirut's southern suburbs overnight.",
     "Israeli jets", "the other wire shape: CITY (Agency) -"),
    ("WASHINGTON, July 3 (AP) — The Senate voted to approve the measure.",
     "The Senate voted", "CITY, Month D (Agency) —"),
    ("According to his information, the outskirts of Volnovakha came under attack",
     "The outskirts", "SHIPPED: the LEAD was a dependent clause about a person not yet named"),
    ("It was reported that the strike damaged the refinery.",
     "The strike damaged", "'it was reported that' says nothing"),
    ("Russian forces set fire to a building of the Kherson Maritime Academy, the vice-rector said.",
     "Russian forces set fire", "GUARD: ordinary prose must pass through untouched"),
    ("The Kremlin said on Monday that talks would resume in Istanbul next week.",
     "The Kremlin said", "GUARD: a real sentence that merely contains 'said' is not padding"),
]

# a lead has to stand on its own — it cannot open by pointing at something unintroduced
STANDALONE_CASES = [
    ("Two civilians were killed and eleven wounded in attacks on the Donetsk region.", True,
     "a complete, self-contained opening line"),
    ("He added that the shelling continued into the evening.", False,
     "'He' — the reader has not met him yet"),
    ("The official said the strikes would continue for several more days.", False,
     "'The official' — which official?"),
]


def main():
    fails = []
    ran = [0]        # guard: every declared case MUST actually execute

    print("\n=== CATEGORY ===")
    for title, want, why in CATEGORY_CASES:
        got = app._classify(title)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("category", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {got:9} (want {want:9}) {title[:44]}")

    print("\n=== GEOLOCATION ===")
    for title, desc, want, why in GEO_CASES:
        r = app._geolocate(title, "", desc)
        got = r[2] if r else None
        if want and want.startswith("!"):        # this place must NOT be the answer (None is fine)
            ok = got is None or want[1:] not in got
        else:
            ok = (want is None and got is None) or (want is not None and got is not None and want in got)
        ran[0] += 1
        if not ok:
            fails.append(("geo", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {str(got)[:24]:26} (want {str(want)[:16]:18}) {title[:34]}")

    print("\n=== FLUFF FILTER ===")
    for title, url, want_drop, why in FLUFF_CASES:
        got_drop = app._is_fluff(title, url)
        ok = got_drop == want_drop
        ran[0] += 1
        if not ok:
            fails.append(("fluff", title, want_drop, got_drop, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'DROP' if got_drop else 'KEEP'} (want {'DROP' if want_drop else 'KEEP'}) {title[:44]}")

    print("\n=== GEOLOCATION (article URL as a country hint) ===")
    for title, desc, url, want, why in GEO_URL_CASES:
        r = app._geolocate(title, "", desc, url)
        got = r[2] if r else None
        if want.startswith("!"):          # this place must NOT be the answer
            ok = got is None or want[1:] not in got
        else:
            ok = got is not None and want in got
        ran[0] += 1
        if not ok:
            fails.append(("geo-url", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {str(got)[:26]:28} (want {want[:18]:20}) {title[:30]}")

    print("\n=== CLIP RELEVANCE (does this clip belong to this story?) ===")
    for ev, clip, want, why in CLIP_CASES:
        got = app._clip_matches(ev, clip)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("clip", ev, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'ATTACH' if got else 'REJECT'} (want {'ATTACH' if want else 'REJECT'}) {clip[:40]}")

    print("\n=== DEDUP (different events must stay separate) ===")
    for a, b, same, why in DEDUP_CASES:
        ka = app._sigwords(a) - app._GENERIC_WORDS
        kb = app._sigwords(b) - app._GENERIC_WORDS
        merged = len(ka & kb) >= 2          # the same-place/same-cat rule in world_events
        ok = merged == same
        ran[0] += 1
        if not ok:
            fails.append(("dedup", a, same, merged, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'MERGE' if merged else 'KEEP '} (want {'MERGE' if same else 'KEEP '}) shared={sorted(ka & kb)}")

    print("\n=== SIMILARITY METER (same story from two sources = one dot) ===")
    for a, b, same, why in SIM_CASES:
        ta, tb = app._norm_tokens(a), app._norm_tokens(b)
        dup = app._same_story(ta, tb)
        shared = len(ta & tb)
        sim = shared / (min(len(ta), len(tb)) or 1)
        ok = dup == same
        ran[0] += 1
        if not ok:
            fails.append(("similarity", a[:48], same, dup, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'DUP ' if dup else 'KEEP'} (want {'DUP ' if same else 'KEEP'}) shared={shared} sim={sim:.2f}")

    print("\n=== STARRED COUNTRIES (FIPS lookup + name matching) ===")
    for name, want in FIPS_CASES:
        got = app._fips_for(name)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("fips", name, want, got, "GDELT sourcecountry: needs the FIPS code, not ISO"))
        print(f"  {'ok ' if ok else 'FAIL'} {name:26} -> {got or '(unsupported)'}")
    for a, b, want in CMATCH_CASES:
        got = app._country_match(a, b)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("cmatch", f"{a} / {b}", want, got, "starred filter must accept naming variants"))
        print(f"  {'ok ' if ok else 'FAIL'} {a} == {b} ? {got} (want {want})")

    print("\n=== VERSION COMPARE (auto-update) ===")
    for remote, local, want in VER_CASES:
        got = app._is_newer(remote, local)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("version", f"{remote} vs {local}", want, got, "update must compare versions numerically"))
        print(f"  {'ok ' if ok else 'FAIL'} {remote} newer than {local}? {got} (want {want})")

    print("\n=== CURRENT LEADERS (name match + Factbook cross-check) ===")
    for a, b, want in NAMEMATCH_CASES:
        got = app._name_match(a, b)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("namematch", f"{a} / {b}", want, got, "a shared surname alone must not merge two people"))
        print(f"  {'ok ' if ok else 'FAIL'} {a} == {b}? {got} (want {want})")
    for cands, fb, want, why in LEADER_PICK_CASES:
        got = app._pick_leader([dict(c) for c in cands], fb)
        gotname = got["name"] if got else None
        ok = gotname == want
        ran[0] += 1
        if not ok:
            fails.append(("leader", fb or "(none)", want, gotname, why))
        print(f"  {'ok ' if ok else 'FAIL'} fb={str(fb) or '-':16} -> {gotname} (want {want})")
    for raw, expname, exptitle in FB_PARSE_CASES:
        nm, ti = app._fb_parse(raw)
        ok = nm.startswith(expname) and ti == exptitle
        ran[0] += 1
        if not ok:
            fails.append(("fbparse", raw[:40], f"{expname}/{exptitle}", f"{nm}/{ti}", "Factbook leader field must parse to name+title"))
        print(f"  {'ok ' if ok else 'FAIL'} '{nm}' [{ti}]")
    for na, qa, nb, qb, want in SAME_PERSON_CASES:
        got = app._same_person(na, qa, nb, qb)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("sameperson", f"{na} / {nb}", want, got,
                          "a shared surname alone must not merge two people; QIDs are authoritative"))
        print(f"  {'ok ' if ok else 'FAIL'} same({na!r},{nb!r})={got} (want {want})")
    _saved_dead = app._DEAD_LEADERS
    for dead, fbname, want, why in DEAD_LEADER_CASES:
        app._DEAD_LEADERS = {dead}                       # in-memory only; don't touch the on-disk list
        got = app._is_dead(fbname)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("dead-leader", f"dead={dead} fb={fbname}", want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} is_dead({fbname!r} | knew {dead!r})={got} (want {want})")
    app._DEAD_LEADERS = _saved_dead
    for raw, want, why in TG_CLEAN_CASES:
        got = app._tg_headline(app._tg_clean(raw))
        ok = want in got
        ran[0] += 1
        if not ok:
            fails.append(("tg-clean", want, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} tg-headline -> {got[:52]!r}")

    # RESILIENCE: when Wikidata is rate-limited (429) and returns NOTHING, country_leaders must still
    # return CLEAN Factbook names (never blank, never the garbled frontend fallback) — and must keep
    # a distinct head of government (Saudi's MBS) rather than collapsing him into the King.
    print("\n=== LEADERS RESILIENT TO WIKIDATA 429 (never blank / garbled again) ===")
    _sa_fb = {"cos": "King and Prime Minister SALMAN bin Abd al-Aziz Al Saud (since 23 January 2015)",
              "hog": "Crown Prince and Prime Minister MUHAMMAD BIN SALMAN Al Saud (since 27 September 2022)"}
    _oe, _os = app._wd_entities, app._wd_search_person
    import os as _os_mod
    try:
        _os_mod.remove(_os_mod.path.join(app.CACHE_DIR, "leaders_Q99999901.json"))   # deterministic: no stale cache
    except Exception:
        pass
    try:
        app._wd_entities = lambda *a, **k: {}          # simulate HTTP 429 — Wikidata gives nothing
        app._wd_search_person = lambda *a, **k: None
        _r = app.Api().country_leaders("Q99999901", "Saudi Arabia", _sa_fb)
    finally:
        app._wd_entities, app._wd_search_person = _oe, _os
    _names = [L.get("name", "") for L in _r.get("leaders", [])]
    _clean = len(_names) == 2 and all(_names) and not any("crown salman" in n.lower() for n in _names)
    ran[0] += 1
    if not _clean:
        fails.append(("leaders-429", "Saudi Arabia", "King + MBS, clean names", str(_names),
                      "a rate-limited fetch must fall back to clean Factbook names, keeping a distinct head of government"))
    print(f"  {'ok ' if _clean else 'FAIL'} rate-limited Saudi -> {_names}")
    for labs, want in LEAN_CASES:
        got = app._lean_from_alignments(labs)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("lean", str(labs)[:36], want, got, "ruling-party alignment must map to a left-right score"))
        print(f"  {'ok ' if ok else 'FAIL'} {str(labs)[:34]:36} -> {got} (want {want})")

    print("\n=== CO-LOCATION COLLAPSE (one dot per place; keep the severest) ===")
    for events, want_n, want_cat, why in COLLAPSE_CASES:
        kept = app._collapse_colocated([dict(e) for e in events])
        ok = len(kept) == want_n and (want_cat is None or (kept and kept[0]["cat"] == want_cat))
        ran[0] += 1
        if not ok:
            fails.append(("collapse", events[0]["title"], f"{want_n}/{want_cat}",
                          f"{len(kept)}/{kept[0]['cat'] if kept else '-'}", why))
        print(f"  {'ok ' if ok else 'FAIL'} {len(kept)} kept (want {want_n}) {events[0]['title'][:34]}")

    print("\n=== CLIP OWNERSHIP (one clip belongs to one dot; token-stable id) ===")
    ran[0] += 1
    if not _clip_assignment_ok():
        fails.append(("clip-owner", "assignment", "owned by best story", "mismatch",
                      "a clip must have a token-stable id and be assigned to its single best dot"))
    print(f"  {'ok ' if _clip_assignment_ok() else 'FAIL'} clip assigned to its best-matching story")

    print("\n=== SHARPEN (wire copy -> the fact in the first words) ===")
    for raw, want, why in SHARPEN_CASES:
        got = app._sharpen(raw)
        ok = got.startswith(want)
        ran[0] += 1
        if not ok:
            fails.append(("sharpen", raw[:48], want, got[:48], why))
        print(f"  {'ok ' if ok else 'FAIL'} {got[:62]}")

    print("\n=== LEAD STANDS ALONE ===")
    for raw, want, why in STANDALONE_CASES:
        got = app._standalone(app._sharpen(raw))
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("standalone", raw[:48], want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'LEAD' if got else 'no  '} {raw[:54]}")

    print("\n=== WIRE CHATTER (drop admin greetings/sign-offs; keep real news) ===")
    for text, want_drop, why in CHATTER_CASES:
        got = app._tg_is_chatter(text)
        ok = got == want_drop
        ran[0] += 1
        if not ok:
            fails.append(("chatter", text[:48], want_drop, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'DROP' if got else 'KEEP'} (want {'DROP' if want_drop else 'KEEP'}) {text[:44]}")

    print("\n=== MAP RELIABILITY (attributed statements reach the map; rumour/speculation don't) ===")
    for text, want_keep, why in RELIABLE_CASES:
        got = app._tg_reliable(text)
        ok = bool(got) == want_keep
        ran[0] += 1
        if not ok:
            fails.append(("reliable", text[:48], want_keep, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {'KEEP' if got else 'DROP'} (want {'KEEP' if want_keep else 'DROP'}) {text[:44]}")

    print("\n=== IMPORTANCE SAFETY NET (_hard_news: casualties/top-official never hidden) ===")
    for title, want_hard, why in HARD_NEWS_CASES:
        got = app._hard_news(title)
        ok = bool(got) == want_hard
        ran[0] += 1
        if not ok:
            fails.append(("hard-news", title[:48], want_hard, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} hard={bool(got)} (want {want_hard}) {title[:44]}")

    print("\n=== MAP-WORTHY GATE (broad feature / minor-local hidden; real scene kept) ===")
    _o_sc, _o_w = app._ai_scope, app._ai_where
    try:
        _weak = (app.COUNTRY_COORDS["Iran"][0], app.COUNTRY_COORDS["Iran"][1], "Iran", "Iran")  # bare centroid
        _city = (50.45, 30.52, "Kyiv, Ukraine", "Ukraine")                                      # a real scene
        app._ai_scope = lambda t: "regional"; app._ai_where = lambda t: ""
        _mw_broad = app._map_worthy("Wars, Wildfires and Migrants Leave Europe Straining", "war in Iran", _weak)
        app._ai_where = lambda t: "Kyiv, Ukraine"
        _mw_scene = app._map_worthy("Russia strikes Kyiv apartment block", "", _city)
        app._ai_scope = lambda t: "local"; app._ai_where = lambda t: ""
        _mw_local = app._map_worthy("Illegal sand extraction erodes a beach", "", _city)
        _mw_cas = app._map_worthy("Gunman kills 9 at a local market", "", _city)   # hard-news override
        app._ai_scope = lambda t: ""
        _mw_new = app._map_worthy("A fresh unsummarised story about a place", "", _city)  # no scope yet -> shown
    finally:
        app._ai_scope, app._ai_where = _o_sc, _o_w
    _mw_ok = (not _mw_broad) and _mw_scene and (not _mw_local) and _mw_cas and _mw_new
    ran[0] += 1
    if not _mw_ok:
        fails.append(("map-worthy", "importance gate",
                      "broad+local DROP; scene/casualty/unrated KEEP",
                      f"broad={_mw_broad} scene={_mw_scene} local={_mw_local} casualty={_mw_cas} new={_mw_new}",
                      "the world map hides broad features & minor-local, keeps real scenes/casualties/unrated"))
    print(f"  {'ok ' if _mw_ok else 'FAIL'} broad={'DROP' if not _mw_broad else 'keep'} scene={'KEEP' if _mw_scene else 'drop'} "
          f"local={'DROP' if not _mw_local else 'keep'} local+casualty={'KEEP' if _mw_cas else 'drop'} unrated={'KEEP' if _mw_new else 'drop'}")

    print("\n=== TERSE STRIKE CLASSIFICATION (firehose posts -> security, no false positives) ===")
    for title, want, why in CLASSIFY_STRIKE_CASES:
        got = app._classify(title)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("classify-strike", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {got:9} (want {want:9}) {title[:38]}")

    print("\n=== TELEGRAM HEADLINES (never cut mid-word; always Capitalised) ===")
    for raw, check, why in HEADLINE_CASES:
        h = app._tg_headline(raw)
        if check == "no_midword":
            core = h.rstrip("… ").rstrip()
            last = core.split()[-1].strip(".,;:!?\"'“”") if core.split() else ""
            got = bool(last) and re.search(r"\b" + re.escape(last) + r"\b", raw) is not None
        elif check == "has_content":            # a lead-in ("<name>:") is not a headline
            got = not h.rstrip().endswith(":") and len(h.split()) >= 6
        elif check == "no_dangle":              # never end on a dangling preposition/article/conjunction
            got = re.search(r"[\s,;:]\b(?:in|on|at|of|to|the|a|an|and|or|nor|with|from|by|into|onto|"
                            r"upon|per|via|amid)\b\.?$", h.strip(), re.I) is None
        else:                                   # "capitalised"
            got = bool(h) and h[0].isupper()
        ran[0] += 1
        if not got:
            fails.append(("headline", raw[:60], check, h[:60], why))
        print(f"  {'ok ' if got else 'FAIL'} {check:12} {h[:52]}")

    print("\n=== CLEAN HEADLINE (strip ' - Outlet', never a compound word) ===")
    for raw, want, why in CLEAN_HEADLINE_CASES:
        got = app._clean_headline(raw)
        if want.startswith("!"):                 # this substring must be GONE
            ok = want[1:] not in got
        else:                                     # this substring must SURVIVE
            ok = want in got
        ran[0] += 1
        if not ok:
            fails.append(("clean-headline", raw[:56], want, got[:56], why))
        print(f"  {'ok ' if ok else 'FAIL'} {got[:60]}")

    print("\n=== DATELINE (the wire states the event location first) ===")
    for title, desc, want, why in DATELINE_CASES:
        r = app._geolocate(title, "", desc)
        got = r[2] if r else None
        ok = got is not None and want in got
        ran[0] += 1
        if not ok:
            fails.append(("dateline", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {str(got)[:24]:26} (want {want[:14]:16}) {title[:32]}")

    print("\n=== DATELINE STRIP (a brief must not open with a wire place-stamp) ===")
    for raw, want, why in DATELINE_STRIP_CASES:
        got = app._strip_promo(raw)
        ok = (got == want)
        ran[0] += 1
        if not ok:
            fails.append(("dateline-strip", raw[:44], want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {got[:52]}")

    print("\n=== TELEGRAM MEDIA (the wire's own photos must actually be extracted) ===")
    for raw, want in CSS_URL_CASES:
        got = app._css_url(raw)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("media", raw, want, got,
                          "the scraper only accepted &#39; — so a PLAIN-quoted url() found nothing, "
                          "and every Telegram album was scraped as 'no media'"))
        print(f"  {'ok ' if ok else 'FAIL'} {raw[:40]:42} -> {got[:36]}")

    print("\n=== MEDIA DEDUP (never the same file twice under one story) ===")
    for urls, want, why in MEDIA_DEDUP_CASES:
        seen, kept = set(), []
        for u in urls:                       # mirrors _push() in Api.event_media
            if u and u not in seen:
                seen.add(u)
                kept.append(u)
        ok = len(kept) == want
        ran[0] += 1
        if not ok:
            fails.append(("media-dedup", str(urls)[:60], want, len(kept), why))
        print(f"  {'ok ' if ok else 'FAIL'} {len(urls)} in -> {len(kept)} out (want {want})")

    print("\n=== FLAGS (who is a PARTY to the event — not who a victim happened to be) ===")
    for title, country, want, why in FLAG_CASES:
        got = app._involved_countries(title, country)
        ok = got == want
        ran[0] += 1
        if not ok:
            fails.append(("flags", title, want, got, why))
        print(f"  {'ok ' if ok else 'FAIL'} {str(got)[:34]:36} (want {str(want)[:24]:26}) {title[:28]}")

    # FLAG COVERAGE: every country the gazetteer can emit MUST resolve to an ISO2 code, or its flag is blank
    # in the UI (this is how Serbia lost its flag). Enforce it so no country ever silently loses its flag.
    print("\n=== FLAG COVERAGE (every gazetteer country resolves to a flag) ===")
    import re as _re, json as _json
    _here = os.path.dirname(os.path.abspath(__file__))
    _gaz = _json.load(open(os.path.join(_here, "cities_gaz.json"), encoding="utf-8"))
    _gc = set()
    for _k, _v in _gaz.items():
        for _rec in _v:
            if len(_rec) >= 3 and _rec[2]:
                _gc.add(_rec[2])
    _html = open(os.path.join(_here, "meridian-relief.html"), encoding="utf-8").read()
    _iso2 = _json.loads(_re.search(r"const ISO2 = (\{.*?\});", _html).group(1))
    _missing = sorted(_gc - set(_iso2.keys()))
    ran[0] += 1
    if _missing:
        fails.append(("flag-coverage", "ISO2 gaps", "0 missing",
                      f"{len(_missing)} missing e.g. {_missing[:6]}",
                      "every country a story can be geolocated to must map to an ISO2 code so its flag renders"))
    print(f"  {'ok ' if not _missing else 'FAIL'} {len(_gc)} gazetteer countries, {len(_missing)} without a flag")

    # PHOTO FALLBACK: a photoless story's hero must never be a country flag/locator map. story_photo()'s
    # country step now shows the country's MAIN CITY instead, so _LARGEST_CITY must be built and map big
    # news countries to a real city (not the country), and _good_img must still reject a flag URL.
    print("\n=== PHOTO FALLBACK (country hero is a city photo, never a flag) ===")
    _lc = getattr(app, "_LARGEST_CITY", {})
    _flagurl = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/aa/Flag_of_Kuwait.svg/1280px-Flag_of_Kuwait.svg.png"
    _pf_ok = (bool(_lc) and _lc.get("Ukraine") == "kyiv" and _lc.get("Kuwait") == "kuwait city"
              and _lc.get("Iran") == "tehran" and not app._good_img(_flagurl))
    ran[0] += 1
    if not _pf_ok:
        fails.append(("photo-fallback", "country hero", "city photo, flag rejected",
                      f"Ukraine->{_lc.get('Ukraine')} Kuwait->{_lc.get('Kuwait')} Iran->{_lc.get('Iran')} flag_good={app._good_img(_flagurl)}",
                      "the no-photo hero must be the country's main city, never its flag"))
    print(f"  {'ok ' if _pf_ok else 'FAIL'} main city Ukraine->{_lc.get('Ukraine')}, Kuwait->{_lc.get('Kuwait')}, "
          f"Iran->{_lc.get('Iran')}; flag rejected: {not app._good_img(_flagurl)}")

    # WIKI THUMBNAIL: a FULL-RES commons original (10-30 MB) never paints in the webview -> a black hero.
    # _wiki_thumb must bound every Wikimedia URL to a <=NNNpx thumbnail (and leave non-wikimedia URLs alone).
    _orig = "https://upload.wikimedia.org/wikipedia/commons/2/2b/Mumbai_Bandra-Worli_Sea_Link.jpg"
    _thm  = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a0/X.jpg/2400px-X.jpg"
    _fp   = "https://commons.wikimedia.org/wiki/Special:FilePath/Y.jpg"
    _nyt  = "https://static01.nyt.com/images/2026/x.jpg"
    _wt_ok = (app._wiki_thumb(_orig, 1280) == "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2b/"
                                              "Mumbai_Bandra-Worli_Sea_Link.jpg/1280px-Mumbai_Bandra-Worli_Sea_Link.jpg"
              and "/1280px-X.jpg" in app._wiki_thumb(_thm, 1280)      # an existing thumb is just resized
              and app._wiki_thumb(_fp, 1280).endswith("?width=1280")  # FilePath gets a bounded width
              and app._wiki_thumb(_nyt, 1280) == _nyt)                # non-wikimedia untouched
    ran[0] += 1
    if not _wt_ok:
        fails.append(("wiki-thumb", "bound to thumbnail", "full-res -> /thumb/../1280px-, others sane",
                      f"orig->{app._wiki_thumb(_orig,1280)}",
                      "a full-res commons original stalls to a black hero in the webview"))
    print(f"  {'ok ' if _wt_ok else 'FAIL'} full-res original -> {app._wiki_thumb(_orig,1280).split('/commons/')[-1][:52]}")

    # AI GEO FALLBACK — OFFLINE INVARIANTS. The AI second opinion must be purely additive: it fires ONLY on
    # a weak rule result (None or a bare country centroid), never on a solid city/scene. These checks touch
    # no network (a non-weak result short-circuits before any LLM call), so they stay deterministic.
    print("\n=== AI GEO FALLBACK (offline invariants) ===")
    _entebbe = (0.056, 32.479, "Entebbe, Uganda", "Uganda")          # a real city scene
    _ug_centroid = app.COUNTRY_COORDS["Uganda"]
    _country_dot = (_ug_centroid[0], _ug_centroid[1], "Uganda", "Uganda")
    _ag_ok = (app._geo_is_weak(None) is True
              and app._geo_is_weak(_entebbe) is False               # specific city -> not weak
              and app._geo_is_weak(_country_dot) is True            # bare country centroid -> weak
              # a solid rule scene is returned UNCHANGED (no AI consulted -> no network)
              and app._locate("Statue unveiled at Uganda's Entebbe Airport", "", "in Entebbe, Uganda", "")[:4]
                  == app._geolocate("Statue unveiled at Uganda's Entebbe Airport", "", "in Entebbe, Uganda", ""))
    ran[0] += 1
    if not _ag_ok:
        fails.append(("ai-geo", "invariants", "weak-only + additive", str(_ag_ok),
                      "the AI fallback must fire only on a weak (None/country-centroid) result, never override a real scene"))
    print(f"  {'ok ' if _ag_ok else 'FAIL'} weak(None)={app._geo_is_weak(None)} weak(city)={app._geo_is_weak(_entebbe)} "
          f"weak(country)={app._geo_is_weak(_country_dot)}; solid scene unchanged")

    # allow_ai=False MUST NOT make a live geolocation call — this is what keeps the cold-start build from
    # stalling on hundreds of network LLM round-trips. With the LLM "available" and _geolocate_ai stubbed,
    # a story the rules can't place must skip the live call when allow_ai=False, and take it when True.
    _orig_geoai, _orig_avail = app._geolocate_ai, app._llm_available
    _ai_calls = [0]
    try:
        app._llm_available = lambda: True
        def _count_geoai(*a, **k):
            _ai_calls[0] += 1
            return ""                                  # no real network; just prove it was (not) called
        app._geolocate_ai = _count_geoai
        app._locate("Markets wobble on mixed signals", "", "", "", allow_ai=False)
        _fast_calls = _ai_calls[0]
        app._locate("Markets wobble on mixed signals", "", "", "", allow_ai=True)
        _slow_calls = _ai_calls[0]
    finally:
        app._geolocate_ai, app._llm_available = _orig_geoai, _orig_avail
    _noai_ok = (_fast_calls == 0 and _slow_calls >= 1)   # skipped when False, taken when True
    ran[0] += 1
    if not _noai_ok:
        fails.append(("ai-geo", "allow_ai gate", "0 live calls when False, >=1 when True",
                      f"fast={_fast_calls} slow={_slow_calls}",
                      "the cold-start build passes allow_ai=False so it never blocks on live geolocation calls"))
    print(f"  {'ok ' if _noai_ok else 'FAIL'} allow_ai=False -> {_fast_calls} live call(s); allow_ai=True -> {_slow_calls}")

    # SOURCE MUTE — a muted outlet is hidden from the map (no dots, no citations), matched on domain / name
    # / url; other outlets are untouched. (The default mutes The Guardian per the user.)
    print("\n=== SOURCE MUTE (a hidden outlet never reaches the map) ===")
    _mute_ok = (app._is_muted("theguardian.com", "The Guardian", "https://www.theguardian.com/world/x")
                and app._is_muted("", "", "https://amp.theguardian.com/x")
                and not app._is_muted("bbc.co.uk", "BBC", "https://bbc.co.uk/x")
                and not app._is_muted("news.antiwar.com", "Antiwar.com", ""))
    ran[0] += 1
    if not _mute_ok:
        fails.append(("mute", "guardian", "guardian hidden, others kept", str(_mute_ok),
                      "a muted source must be filtered on domain/name/url, and only that source"))
    print(f"  {'ok ' if _mute_ok else 'FAIL'} guardian muted; bbc/antiwar kept; mutes={sorted(app._muted_sources())}")

    # SAME-EVENT MERGE — three outlets covering one event become ONE dot that cites them all (first
    # reporter as the primary), while a genuinely different same-country event stays separate.
    print("\n=== SAME-EVENT MERGE (dedupe coverage into one cited dot) ===")
    _K = (50.45, 30.52)
    _kyiv = [
        {"title": "Kyiv: 9 dead and 22 hurt in missile attack", "place": "Kyiv, Ukraine",
         "country": "Ukraine", "lat": _K[0], "lng": _K[1], "hrs": 12.6, "source": "France 24",
         "cat": "security", "image": "x.jpg", "sum": "Nine killed in Kyiv.", "url": "u1"},
        {"title": "Russian missile attacks on Ukraine last night left nine civilians dead, and 30+ injured",
         "place": "Ukraine", "country": "Ukraine", "lat": _K[0], "lng": _K[1], "hrs": 9.6,
         "source": "Rerum Novarum", "cat": "security", "image": "", "sum": "Nine civilians dead.", "url": "u2"},
        {"title": "Overnight Russian barrage kills nine in Kyiv, as air defense struggles", "place": "Kyiv, Ukraine",
         "country": "Ukraine", "lat": _K[0], "lng": _K[1], "hrs": 3.4, "source": "NPR", "cat": "security",
         "image": "y.jpg", "sum": "Missiles hit five districts.", "url": "u3"},
        {"title": "Drone strike on Odesa port damages grain terminal", "place": "Odesa, Ukraine",
         "country": "Ukraine", "lat": 46.48, "lng": 30.72, "hrs": 6.0, "source": "Reuters",
         "cat": "security", "image": "", "sum": "A drone hit the port.", "url": "u4"},
    ]
    _m = app._merge_same_event([dict(e) for e in _kyiv])
    _byplace = {e["place"]: e for e in _m}
    _kdot = next((e for e in _m if e["place"] == "Kyiv, Ukraine"), None)
    _me_ok = (len(_m) == 2                                                # 3 Kyiv reports -> 1, + Odesa
              and _kdot is not None and len(_kdot.get("sources", [])) == 3
              and _kdot["sources"][0]["name"] == "France 24"             # first reporter is the primary source
              and _kdot["title"].startswith("Kyiv: 9 dead")             # ...and its headline leads
              and _kdot["hrs"] == 3.4                                    # dot stays fresh (latest update)
              and app._death_toll("barrage kills nine in Kyiv") == 9
              and app._death_toll("markets rose 3 percent today") is None)
    ran[0] += 1
    if not _me_ok:
        fails.append(("merge", "kyiv-3-sources", "1 dot, 3 sources, France 24 first",
                      f"dots={len(_m)} kdot_sources={len(_kdot.get('sources', [])) if _kdot else 0}",
                      "three outlets covering '9 dead in Kyiv' must merge into one cited dot; Odesa stays separate"))
    print(f"  {'ok ' if _me_ok else 'FAIL'} {len(_m)} dots; Kyiv sources="
          f"{[s['name'] for s in (_kdot.get('sources', []) if _kdot else [])]}")

    # FIRST-REPORTER PROMOTION — the inline dedup adds the FRESHEST copy first, then cites older copies onto it.
    # A story broken by an outlet 12h ago and re-run by another 4h ago must stay attributed to whoever broke it,
    # not the later re-run. _cite_source promotes the earlier reporter's outlet + headline to the shown primary.
    _prim = {"source": "Aa", "domain": "aa.com", "url": "https://aa.com/x",
             "title": "4 Palestinians injured in West Bank raid", "sum": "later re-run text", "hrs": 4.1}
    _older = {"source": "Middle East Monitor", "domain": "middleeastmonitor.com", "url": "https://memo.org/y",
              "title": "Israeli forces injure four in West Bank", "sum": "the outlet that broke it", "hrs": 12.2}
    app._cite_source(_prim, _older)
    _fr_ok = (_prim["source"] == "Middle East Monitor"          # first reporter now leads the byline
              and _prim["title"].startswith("Israeli forces")   # ...with its headline
              and _prim["hrs"] == 4.1                            # dot timestamp stays freshest
              and len(_prim.get("sources", [])) == 2)           # both outlets cited
    ran[0] += 1
    if not _fr_ok:
        fails.append(("merge", "first-reporter-promotion", "Middle East Monitor leads",
                      f"source={_prim.get('source')} sources={len(_prim.get('sources', []))}",
                      "an earlier-reporting cited outlet must become the shown primary"))
    print(f"  {'ok ' if _fr_ok else 'FAIL'} first reporter promoted -> primary={_prim['source']}")

    # CASUALTY FINGERPRINT — two reports that match on BOTH killed AND injured are the same incident even
    # when they sit far apart with different wording (one on 'Black Sea', one on the named town). No geo
    # constraint, so a merge the plain same-place rule can never make. Also guards the injured extractor.
    print("\n=== CASUALTY FINGERPRINT MERGE (killed + injured, far apart) ===")
    _inj_ok = (app._injured_toll("The center said 40 people were injured") == 40
               and app._injured_toll("Seven killed and 40 injured") == 40
               and app._injured_toll("wounded nine soldiers") == 9
               and app._injured_toll("Twenty-one people hospitalized") is None   # compound: safe, never a wrong 1
               and app._injured_toll("no casualties") is None)
    _bs = [
        {"title": "Russia says civilians killed in strike on Black Sea resort", "cat": "security",
         "sum": "At least seven people were killed and 40 injured by a Ukrainian strike.",
         "place": "Black Sea", "country": "Russia", "lat": 43.4, "lng": 34.3, "hrs": 1.8,
         "source": "Deutsche Welle", "url": "bs1", "image": ""},
        {"title": "Seven people killed in drone attack on Gelendzhik", "cat": "security",
         "sum": "Seven people were killed and 40 injured in the attack.",
         "place": "Gelendzhik, Russia", "country": "Russia", "lat": 44.58, "lng": 38.07, "hrs": 0.6,
         "source": "TASS", "url": "bs2", "image": "img"},
    ]
    _mbs = app._merge_same_event([dict(e) for e in _bs])
    _fp_ok = _inj_ok and len(_mbs) == 1 and len(_mbs[0].get("sources", [])) == 2
    ran[0] += 1
    if not _fp_ok:
        fails.append(("merge", "casualty-fingerprint", "1 dot citing 2 sources",
                      f"inj_ok={_inj_ok} dots={len(_mbs)}",
                      "7 killed + 40 injured in two reports 4 deg apart must merge on the two-number fingerprint"))
    print(f"  {'ok ' if _fp_ok else 'FAIL'} {len(_bs)} reports -> {len(_mbs)} dot; injured extractor sane")

    # SEMANTIC DEDUP (the AI net) — folds a same-event pair the code can't prove: no shared distinctive
    # words, no toll on the vague copy. Purely additive (no LLM -> untouched) and conservative (different
    # topics stay apart). The verdict is stubbed so the test is offline + deterministic.
    print("\n=== SEMANTIC DEDUP (AI net, stubbed verdict) ===")
    _sd = [
        {"title": "Russia says civilians killed in strike on Black Sea resort", "cat": "security",
         "sum": "Moscow accused Kyiv of stepping up attacks.", "place": "Black Sea", "country": "Russia",
         "lat": 43.4, "lng": 34.3, "hrs": 1.8, "source": "DW", "url": "s1", "image": "",
         "sources": [{"name": "DW", "url": "s1", "hrs": 1.8}]},
        {"title": "IN BRIEF: Seven killed in drone attack on Gelendzhik", "cat": "security",
         "sum": "40 injured.", "place": "Gelendzhik, Russia", "country": "Russia",
         "lat": 44.58, "lng": 38.07, "hrs": 0.6, "source": "TASS", "url": "s2", "image": "img",
         "sources": [{"name": "TASS", "url": "s2", "hrs": 0.6}]},
        {"title": "Alibaba unveils its most powerful AI model", "cat": "tech", "sum": "Shares jumped.",
         "place": "China", "country": "China", "lat": 35.0, "lng": 105.0, "hrs": 3.4, "source": "CNBC",
         "url": "s3", "image": "", "sources": [{"name": "CNBC", "url": "s3", "hrs": 3.4}]},
    ]
    _orig_same, _orig_avail = app._ai_same_event, app._llm_available
    try:
        app._llm_available = lambda: True
        app._ai_same_event = lambda a, b: ("black sea" in (a["title"] + b["title"]).lower()
                                           and "gelendzhik" in (a["title"] + b["title"]).lower())
        _out = app._ai_dedup([dict(e) for e in _sd])
        _surv = next((e for e in _out if e["country"] == "Russia"), None)
        _sem_ok = (len(_out) == 2 and _surv is not None and _surv.get("image") == "img"     # keep the pictured dot
                   and _surv.get("place") == "Gelendzhik, Russia"                            # ...at the named town
                   and len(_surv.get("sources", [])) == 2                                    # DW cited on it
                   and any("Alibaba" in e["title"] for e in _out))                           # different topic kept
        app._llm_available = lambda: False
        _noop = len(app._ai_dedup([dict(e) for e in _sd])) == 3                              # no LLM -> untouched
    finally:
        app._ai_same_event, app._llm_available = _orig_same, _orig_avail
    _sem_ok = _sem_ok and _noop
    ran[0] += 1
    if not _sem_ok:
        fails.append(("dedup", "ai-semantic-net", "Black Sea folds into Gelendzhik; Alibaba stays; offline no-op",
                      f"dots={len(_out)} noop3={_noop}",
                      "the LLM net must fold a proven same-event pair, keep different topics apart, "
                      "and do nothing without an LLM"))
    print(f"  {'ok ' if _sem_ok else 'FAIL'} {len(_sd)} -> {len(_out)} dots; offline no-op={_noop}")

    # WATER PLACE never collapses — a sea/ocean is a huge AREA, so two unrelated stories that both fell back
    # to 'Black Sea' (a resort strike + a refinery note naming 'Black Sea Petroleum') must stay two dots.
    print("\n=== WATER PLACE NOT COLLAPSED (a sea is an area, not a spot) ===")
    _wp_ok = (app._is_water_place("Black Sea") and app._is_water_place("Sea of Azov")
              and app._is_water_place("Persian Gulf") and app._is_water_place("Kerch Strait")
              and not app._is_water_place("Swansea") and not app._is_water_place("Gelendzhik, Russia"))
    _bw = [
        {"title": "Russia says civilians killed in strike on Black Sea resort", "cat": "security",
         "place": "Black Sea", "country": "Russia", "lat": 43.4, "lng": 34.3, "hrs": 1.8,
         "source": "DW", "url": "w1", "image": ""},
        {"title": "Georgia's Kulevi refinery diversifies from Russian crude, Black Sea Petroleum says",
         "cat": "economy", "place": "Black Sea", "country": "Russia", "lat": 43.4, "lng": 34.3, "hrs": 2.0,
         "source": "Reuters", "url": "w2", "image": ""},
    ]
    _wc = app._collapse_colocated([dict(e) for e in _bw])
    _wp_ok = _wp_ok and len(_wc) == 2
    ran[0] += 1
    if not _wp_ok:
        fails.append(("collapse", "water-not-collapsed", "2 dots (a sea is not one spot)",
                      f"dots={len(_wc)}",
                      "a resort strike and a refinery note both pinned to 'Black Sea' must NOT merge"))
    print(f"  {'ok ' if _wp_ok else 'FAIL'} resort + refinery on Black Sea -> {len(_wc)} dots")

    total = (4 + len(CATEGORY_CASES) + len(GEO_CASES) + len(GEO_URL_CASES) + len(FLUFF_CASES)
             + len(DEDUP_CASES) + len(SIM_CASES) + len(FIPS_CASES) + len(CMATCH_CASES) + len(VER_CASES)
             + len(NAMEMATCH_CASES) + len(LEADER_PICK_CASES) + len(FB_PARSE_CASES) + len(LEAN_CASES)
             + len(SAME_PERSON_CASES) + len(DEAD_LEADER_CASES) + len(TG_CLEAN_CASES) + 1

             + len(CLIP_CASES) + len(HEADLINE_CASES) + len(DATELINE_CASES) + len(DATELINE_STRIP_CASES)
             + len(FLAG_CASES) + len(CSS_URL_CASES) + len(MEDIA_DEDUP_CASES)
             + len(CLEAN_HEADLINE_CASES) + len(COLLAPSE_CASES) + len(CLASSIFY_STRIKE_CASES)
             + len(CHATTER_CASES) + len(RELIABLE_CASES) + len(HARD_NEWS_CASES) + len(SHARPEN_CASES) + len(STANDALONE_CASES) + 1
             + 1   # + flag-coverage one-off
             + 1   # + allow_ai gate (cold-start build makes no live geo calls)
             + 1   # + _wiki_thumb bounds Wikimedia URLs to a thumbnail
             + 1   # + map-worthy importance gate (broad-feature / local drop)
             + 3    # + casualty-fingerprint merge + AI semantic-dedup net + water-not-collapsed
             + 1)   # + first-reporter promotion (inline dedup keeps whoever broke it as the primary)
    print("\n" + "=" * 70)
    # THE GUARD, FINALLY WIRED UP. `ran` was declared to prove every declared case actually executes,
    # and then never checked — so HEADLINE_CASES and DATELINE_CASES sat here for months, counted in
    # the total, printed as "PASSED", and NEVER RUN. A test that does not run is worse than no test:
    # it reports safety it is not providing.
    if ran[0] != total:
        print(f"HARNESS BUG: {total} cases declared, but only {ran[0]} actually executed.")
        print("A declared case list has no loop running it. Do not trust this run.")
        return 1
    if fails:
        print(f"{len(fails)} of {total} FAILED:\n")
        for kind, title, want, got, why in fails:
            print(f"  [{kind}] {title[:60]}")
            print(f"        wanted {want!r}, got {got!r}")
            print(f"        this case exists because: {why}\n")
        return 1
    print(f"ALL {total} PASSED  (category / geo / dateline / headline / fluff / dedup / clips / flags)")
    return 0


if __name__ == "__main__":
    sys.exit(main())