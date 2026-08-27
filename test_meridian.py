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
    # A non-violent ESPIONAGE/SURVEILLANCE story is politics (foreign interference), NOT the red strike bucket —
    # even when the copy says "mercenary"/"operation". SHIPPED BUG: it showed as a red security dot.
    ("UAE-funded mercenary targeted British activist in covert London surveillance operation", "politics",
     "covert surveillance with no violence -> foreign-interference politics, not red security"),
    ("Wagner mercenaries launch an assault on the town, killing dozens", "security",
     "a mercenary story WITH violence stays security — the downgrade only fires when there's no violence"),
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
    # A LEGAL RULING is located at its jurisdiction — the court's country, not a place inside the
    # defendant's NAME. SHIPPED BUG: "Palestine Action ... UK judge rules" dotted PALESTINE (leftmost,
    # part of the group's name) instead of the UK where the court sat. "<Country> judge/court" = the seat.
    ("Palestine Action 'Barclays five' won't face terrorism sentences, UK judge rules", "",
     "United Kingdom", "a UK court ruling dots the UK, not 'Palestine' inside the group's name"),
    ("Palestine Action 'Barclays five' won't face terrorism sentences, UK judge rules", "",
     "!Palestine", "...and it must NOT land on Palestine"),
    # A CURRENCY PREFIX is not the country. SHIPPED BUG: an Indonesian ministry's aid "(approx US$48,500)"
    # dotted the United States — "US$" tokenised to a bare "US".
    ("Ministry earmarks emergency aid for flood victims in Jakarta",
     "The ministry pledged approximately US$48,500 in relief for the affected schools", "Jakarta",
     "US$ is a currency, not the country US — Jakarta wins"),
    ("Ministry earmarks emergency aid for flood victims in Jakarta",
     "The ministry pledged approximately US$48,500 in relief for the affected schools", "!United States",
     "...and it must NOT land on the US"),
    # A strike hitting "X's sites" is an event IN X, not a US action. SHIPPED BUG: "US-Israeli strikes hit
    # Iran's nuclear, medical sites" dotted Washington — "Iran's" sank as a possessive so the attacker won.
    ("US-Israeli strikes hit Iran's nuclear, medical sites", "", "Iran",
     "a strike hitting X's sites dots X (the scene), not the attacker"),
    ("US-Israeli strikes hit Iran's nuclear, medical sites", "", "!United States",
     "...never the attacker's country"),
    ("Russia's attack on Zaporizhzhia kills nine", "", "Zaporizhzhia",
     "GUARD: 'Russia's ATTACK' (no strike verb before Russia) still sinks the actor -> Zaporizhzhia"),
    # "Jackson Hole" is the Fed's Wyoming symposium — bare "Jackson" matched the bigger Jackson, MISSISSIPPI.
    ("Bond market anxiety raises stakes for Warsh's debut Jackson Hole speech", "", "Jackson Hole",
     "SHIPPED BUG: dotted Jackson, Mississippi (~1,800 km off) instead of Jackson Hole, Wyoming"),
    # "Republic" is a word in dozens of country names, never a scene on its own. SHIPPED BUG: a Türkiye
    # government statement dotted "Republic, Missouri". Vetoed as a bare town -> the statement dots Turkey.
    ("The Republic of Turkiye Directorate of Communications: The Israeli PM Office's rant targeting our "
     "President Recep Tayyip Erdogan", "", "Turkey", "a Turkish government statement dots Turkey, not 'Republic, US'"),
    ("The Republic of Turkiye Directorate of Communications targeting our President Erdogan", "",
     "!Republic", "...and bare 'Republic' must never be the scene"),
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
    # The US Federal Reserve. The full name already resolved; the bare wire form "Fed" did not, so an
    # Anadolu-filed "Fed officials signal rate hike" dotted TURKEY. "Fed" only counts with a money-policy word.
    ("Fed officials signal a rate hike if inflation stays elevated", "", "United States",
     "SHIPPED BUG: 'Fed' (the central bank) + 'rate hike' is a Washington story — Anadolu-sourced, dotted Turkey"),
    # SHIPPED BUG: an OUTLET name that contains a place set the scene. "Wall Street Journal, citing a US Army
    # official…" dotted the NYC financial district; the paper is the reporter, the U.S. Army is the subject.
    ("Wall Street Journal, citing a statement by a U.S. Army official, reports the U.S. Army is phasing out a drone battalion",
     "", "United States", "the WSJ is the reporter; the U.S. Army is the subject -> the US, NOT 'Wall Street'"),
    ("The Wall Street Journal reports Japan will raise interest rates next month", "", "Japan",
     "stripping the outlet name lets the real subject (Japan) win instead of Wall Street"),
    ("Protesters march on Wall Street over the bank bailouts", "", "United States",
     "a GENUINE Wall Street mention still resolves — only the outlet name is neutralised"),
    # SHIPPED BUG: the accused BACKER of a plot dotted instead of the scene. "UAE funded plot… MAB urge UK
    # government" dotted the UAE (the accused), but the British group's appeal happens in the UK.
    ("After revelation of UAE funded plot against its former president, MAB urge UK government to take a firm stand",
     "", "United Kingdom", "'UAE funded plot' = the accused sponsor, not the scene; the UK appeal is the event"),
    ("Iran-backed militia launches rockets at Israel", "", "Israel",
     "the backer (Iran) is dropped; the attack LANDS in Israel"),
    ("Saudi-led coalition strikes Houthi positions in Yemen", "", "Yemen",
     "'Saudi-led' is the sponsor; the strike scene is Yemen"),
    ("Russia backed the ceasefire proposal at the United Nations", "", "!Israel",
     "'backed' here is a VERB (Russia is the actor), so Russia is NOT dropped as a fake backer"),
    # An EU leader's STATEMENT is EU news (Brussels), not news of the country they're talking ABOUT. SHIPPED
    # BUG: "von der Leyen said Russia is losing" dotted RUSSIA. The EU institutions map to Belgium (Brussels).
    ("EU Commission President Ursula von der Leyen said Russia is losing the war it started", "", "Belgium",
     "the EU Commission President is a Brussels/Belgium speaker; Russia is only the topic"),
    ("EU Commission announces a new sanctions package on Russia", "", "Brussels",
     "the EU Commission acting is Brussels news, not Russia"),
    # A US official's STATEMENT about Iran is news at their SEAT (the US), not Tehran. SHIPPED BUG: "Treasury
    # Secretary Scott Bessent on Iran says…" dotted Iran. Bessent + Treasury Secretary added to the officials.
    ("Treasury Secretary Scott Bessent on Iran says we are going to have the toughest sanctions in history",
     "", "United States", "the US Treasury Secretary speaking about Iran is Washington news, not Tehran"),
    # A leader RETURNING home from abroad is news in their OWN country, not where they were. SHIPPED BUG:
    # "Cameroon's President returns from … Switzerland" dotted Switzerland (where he had been staying).
    ("Cameroon's Aging President Returns From Monthslong Stay Abroad",
     "Paul Biya, 93, had been in Switzerland since early June. The absence stirred questions about succession.",
     "Cameroon", "'had been in Switzerland' is the past/origin place; the subject's own country (Cameroon) wins"),
    ("FOMC minutes reveal a split over the pace of interest-rate cuts", "", "United States",
     "FOMC is unambiguously the Federal Reserve's rate-setting committee"),
    ("Volunteers fed thousands of stranded travellers during the storm", "", "!United States",
     "'fed' the VERB with no money-policy word is not the Federal Reserve — must not fly to the US"),
    ("ECB signals a rate hike as eurozone inflation cools", "", "!United States",
     "another central bank's rate story must never be captured by the Fed recogniser"),
    ("Trump considers 'massive attack' on Iran as tensions rise", "", "United States",
     "SHIPPED BUG: a leader only CONSIDERING a strike dotted the target — it's news at their seat"),
    ("Trump visits Israel for a peace summit", "", "Israel",
     "...but a leader VISITING a place is AT that place (going-verbs are not deliberation)"),
    ("Rubio says the US is ready to help end the war in Ukraine", "", "United States",
     "a US official's STATEMENT about a foreign country is at their seat, even when the topic is 'located'"),
    ("The same hostage economics playbook, from Havana to Tehran to Beirut",
     "When Israel launched its campaign on Lebanon in March, warplanes targeted civilian banks in Beirut.", "Beirut",
     "a bare rhetorical LIST of cities ('from Havana to Tehran to Beirut') is not a scene — read the body, "
     "which centers on Beirut, instead of grabbing the first-listed Havana"),
    ("Fire broke out at Rosneft's Komsomolsk-on-Amur refinery in Khabarovsk Krai, around 6,500 km from Ukraine "
     "and far beyond demonstrated Ukrainian UAV strike ranges", "", "Khabarovsk",
     "SHIPPED BUG: the bare word 'Amur' matched Amur, INDIA and nothing pinned the real region. The named "
     "refinery city is now in the gazetteer, so the located 'in Khabarovsk Krai' region wins — not a stray"),
    ("Siding with West in conflict with Russia unacceptable for Serbs", "", "Serbia",
     "SHIPPED BUG: dotted Russia (Moscow centroid). A country named only as the OTHER side of a conflict "
     "('conflict WITH Russia') is a party, not the scene — the subject is Serbs -> Serbia"),
    ("Kremlin says the war with Ukraine will continue", "", "Russia",
     "GUARD: the adversary-party rule must not demote when the ACTOR (Kremlin) is the pick — stays Russia"),
    ("North Korea tests missile ahead of US-South Korea drills", "", "North Korea",
     "SHIPPED BUG: dotted the United States. A weapons TEST is at the testing country — 'North Korea' is the "
     "actor (verb 'tests' between it and 'missile'), not the missile's nationality, and beats a higher-profile "
     "country the headline merely names"),
    ("Ivory Coast has acquired at least two Chinese-made Wing Loong II drones", "Ivory Coast bought drones.",
     "Ivoire", "SHIPPED BUG: the bare direction word 'West' dotted West, TEXAS — 'west' is never the scene"),
    ("Trump to Axios: \"We are low-keying it\" with Iran", "", "United States",
     "SHIPPED BUG: a leader's on-record COLON statement (no say-verb) dotted the topic, Iran — it's a "
     "Washington story, and now shares the seat with the other coverage so the two merge into one dot"),
    ("Trump: We will not allow Iran to build a bomb", "", "United States",
     "a bare 'Leader: <quote about a foreign country>' is a statement at their seat, not the foreign topic"),
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
    ("FP-1 drones hitting the Syzran oil refinery in Russia's Samara region", "", "Syzran Refinery",
     "SHIPPED BUG: 'in RUSSIA's Samara region' grabbed the preposition -> dot on Moscow ('oil refinery'->'refinery' norm)"),
    ("Ukrainian drone strike hits the Omsk oil refinery", "", "Omsk Refinery", "facility, not city centre ('oil refinery' normalised to 'refinery')"),
    # A strike on a NAMED refinery lands AT the refinery, not in the ACTOR's country. SHIPPED BUG: "Ukraine
    # struck the Afipsky OIL refinery" dotted UKRAINE — the "oil" qualifier broke the "afipsky refinery" match.
    ("Ukraine struck the Afipsky oil refinery on August 25", "", "Afipsky Refinery",
     "the site is in Russia (Krasnodar), not Ukraine the attacker; the 'oil refinery' norm restores the match"),
    ("Ukraine struck the Afipsky oil refinery on August 25", "", "!Ukraine", "...and it must NOT dot the attacker"),
    # A named REGION must win over the capital. SHIPPED BUG: "in Ukraine's Mykolaiv region" dotted KYIV — the
    # common EN spelling "Mykolaiv" was missing from the gazetteer (only "Mykolayiv"/"Nikolaev" were listed).
    ("A Russian drone struck a suburban passenger train in Ukraine's Mykolaiv region, damaging several carriages",
     "According to Ukrzaliznytsia. No one was injured.", "Mykolaiv", "the named region, not the capital Kyiv"),
    # COMPOUND / ACCENTED country names: a hyphen or a stripped accent must not collapse them to a namesake.
    ("Coup leaders detain president in Guinea-Bissau", "", "Guinea-Bissau",
     "SHIPPED BUG: 'Guinea-Bissau' tokenised to guinea+bissau and dotted GUINEA (a different country)"),
    ("Cote d Ivoire holds a presidential election", "", "Ivoire",
     "SHIPPED BUG: the accent in 'Cote d'Ivoire' had become a SPACE in the alias, so it matched nothing"),
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
    ("Loud explosion, fire in Saudi Arabia's Jubail Industrial City amid reports gas facilities were targeted",
     "", "Jubail", "SHIPPED: 'Jubail' (Al Jubail, a 380k Gulf-coast energy hub) is missing from GeoNames, so a "
     "gas-strike story dropped on the Riyadh centroid — added to _MANUAL_PLACES with the Gulf energy cities"),
    ("JRS launches project to strengthen oil spill resilience in Akwa Ibom community",
     "A community in Nigeria is getting help to deal with oil spills. The initiative is backed by CARITAS Canada.",
     "Nigeria", "SHIPPED: dotted CANADA off 'CARITAS Canada' — an aid group's home country is not the scene; "
     "the located 'in Nigeria' must win (the AI WHERE prompt now says the same for the summary-pass pinpoint)"),
    ("Israel's democracy is fraying, a new ToI poll finds", "",
     "Israel", "SHIPPED BUG: 'ToI' (an abbreviation of The Times of Israel) matched the village of Toi, Japan — "
     "'toi' is now a NEVER-city word, so the story dots Israel from its own name"),
    ("Kennedy Center reopens after renovation", "",
     "United States", "SHIPPED BUG: 'Kennedy' matched Kennedy, Colombia; the Kennedy Center is in Washington — "
     "'kennedy' is a NEVER-city word and 'kennedy center' is in _MANUAL_PLACES"),
    ("Attacks on Zaporozhye NPP expose true nature of Kiev regime", "",
     "Zaporizhzhia", "SHIPPED BUG: dotted KIEV. NER mislabelled 'Zaporozhye NPP' as a two-word PERSON and the "
     "covers_more veto deleted it even though 'Attacks ON Zaporozhye' locates it — leaving only 'Kiev regime', "
     "an attributive label, as the scene. The located facility (the plant) must win, not the government's seat"),
    ("The past two weeks have seen a marked increase in Israeli military activity in south Lebanon", "",
     "Lebanon", "SHIPPED BUG: dotted the village of South Lebanon, OHIO (pop 4,346) and labelled it 'South "
     "Lebanon, United States'. 'south lebanon'/'southern lebanon' are curated to the Lebanon war zone, and the "
     "compass+country guard sends any '<compass> <country>' US town to the country when the story names it"),
    ("Russia is shipping drone parts to Iran through the Caspian Sea, helping Tehran replenish its stockpiles", "",
     "Caspian Sea", "SHIPPED BUG: dotted Washington/Tehran. A transit 'through the Caspian Sea' physically "
     "happens ON the sea — 'through'/'via' are now locating prepositions, so the water route is the scene, not "
     "the sender/receiver/background actor"),
    ("China Added 200,000 Bpd To Crude Reserves In July Despite Hormuz Crisis", "",
     "China", "SHIPPED BUG: dotted the Strait of Hormuz. 'DESPITE the Hormuz crisis' is a contrasting BACKDROP, "
     "not the scene — the event is China's reserve build. Backdrop places (despite/amid) sink to the subject"),
    ("Trump's collapsing popularity exposes strategic failure in the war against Iran",
     "President Donald Trump's approval rating fell to a historic low of 33 percent, a measure of the failure of the joint US-Israeli military strategy against Iran.",
     "United States", "SHIPPED BUG: dotted Tehran. 'the WAR against Iran' makes Iran the ADVERSARY of an abstract "
     "struggle, not a physical scene — so the desc's real subject (the US, Trump's approval) wins"),
    ("US launches air strike against Iran nuclear site", "",
     "Iran", "GUARD: a PHYSICAL 'strike against Iran' still lands IN Iran — only an ABSTRACT 'war/strategy "
     "against X' sinks the target; a strike keeps it as the scene"),
    ("Malaysia fears military miscalculation near Sabah amid US-China rivalry", "",
     "Malaysia", "SHIPPED BUG: dotted CHINA. The scene is 'near Sabah' (a Malaysian Borneo state now in the "
     "gazetteer); the US and China are a rivalry named as context ('amid US-China rivalry'), not the scene"),
    ("Russian Forces Capture Malaya Tokmachka", "",
     "Ukraine", "SHIPPED BUG: dotted MALAYA, PHILIPPINES — the wire's RU spelling 'Malaya Tokmachka' matched a "
     "Philippine town. The Zaporizhzhia-front village (RU Malaya / UA Mala) is now aliased to Ukraine"),
    # A pro-/anti-<COUNTRY> STANCE is a modifier on a person/actor, not the scene. SHIPPED BUG: "AIPAC brand
    # turns toxic as pro-Israel Republican asks lobby to stay out of Michigan race" dotted ISRAEL; the story is
    # a Michigan primary. "pro-Israel" sinks like an adjective so the real scene (Michigan) wins.
    ("AIPAC brand turns toxic as pro-Israel Republican asks lobby to stay out of Michigan race",
     "A Republican Senate candidate asked the American Israel Public Affairs Committee not to run ads in the Michigan race",
     "Michigan", "SHIPPED BUG: 'pro-Israel' (a stance) dotted Israel; the event is the Michigan primary"),
    ("AIPAC brand turns toxic as pro-Israel Republican asks lobby to stay out of Michigan race", "",
     "!Israel", "...and it must NOT land on Israel"),
    # A THREATENED / POTENTIAL strike is an intention voiced BY the threatener — dot the speaker's country, not
    # the target. SHIPPED BUG: "US could carry out further strikes on Iran if needed: Hegseth" dotted TEHRAN.
    # A threat modal (could/would/threatens/vows…) sinks the named target so the actor's country wins.
    ("US could carry out further strikes on Iran if needed: Hegseth", "",
     "United States", "SHIPPED BUG: a THREATENED strike ('could... on Iran') dotted Tehran; the speaker is the US"),
    ("US could carry out further strikes on Iran if needed: Hegseth", "",
     "!Iran", "...and it must NOT land on Iran/Tehran"),
    ("US threatens to strike Iran if talks fail", "",
     "United States", "a threatened strike (direct object 'strike Iran') still dots the threatener, the US"),
    ("US strikes Iran nuclear sites", "",
     "Iran", "GUARD: an ACTUAL strike (no threat modal) still lands IN Iran — the threat sink must not misfire"),
    ("Russia strikes Kyiv overnight", "",
     "Kyiv", "GUARD: an actual overnight strike still dots the scene (Kyiv), not the actor"),
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
    ("Imran Khan transfer to hospital blocked as Pakistan court orders a medical panel",
     "Israel Supreme Court unanimously overturned the government decision to shut down Army Radio, ruling it was driven by improper political considerations.",
     False, "SHIPPED BUG: an ISRAEL clip attached to a PAKISTAN court story on the shared 'Supreme Court' — a generic institution name distinguishes nothing; different countries, different event."),
    ("The JNIM has also attacked VDP outposts in Burkina Faso this weekend, seizing control of one in Konkoura and pillaging two others in northern Burkina Faso.",
     "At least 27 people were killed after a massive fire engulfed a pub in northern Bangkok shortly after midnight on Monday.",
     False, "SHIPPED BUG: attached on {'control','northern'} — 'seizing CONTROL...NORTHERN Burkina Faso' vs 'brought under CONTROL...NORTHERN Bangkok'. Pure coincidence."),
    ("Trump slams California plan to raise the minimum wage for fast-food workers",
     "BREAKING - Trump says Israel 'very happy' about Hamas disarmament deal",
     False, "SHIPPED BUG: a ubiquitous shared name (Trump) + same country (US) pulled an unrelated Hamas clip onto a California min-wage dot. A shared name needs a shared TOPIC, not just the same country."),
    ("Trump keeps embracing data centers even as they become toxic in midterm races",
     "US, Canada race to finalize trade deal ahead of Trump deadline", False,
     "SHIPPED BUG: attached on Trump + the coincidental word 'race' ('midterm RACES' vs 'RACE to finalize'). When the ONLY shared name is a ubiquitous figure, ONE weak word is not the same event."),
    ("Supermarket in Zaporozhye Region attacked by Ukrainian drone",
     "FP-1 strike drones maneuvering before hitting the Syzran oil refinery in Russia's Samara region",
     False, "SHIPPED BUG: attached on {attack, drone, region} — conflict filler"),
    ("US plans $725 million payment towards its large UN debt",
     "The U.S. Justice Department has reached a $400 million settlement with TikTok and parent company "
     "ByteDance over children privacy laws, Axios reports.", False,
     "SHIPPED BUG (live wire): a TikTok settlement post opened a UN-debt dot on the ONLY shared word "
     "'million' + same country (US). A shared magnitude is not a subject (_MONEY_GENERIC)."),
    ("US reaches $400M settlement with TikTok and ByteDance over child privacy",
     "The U.S. Justice Department has reached a $400 million settlement with TikTok and parent company "
     "ByteDance over children privacy laws, Axios reports.", True,
     "...but the SAME TikTok settlement, differently worded, must still attach on {tiktok, bytedance}."),
    ("Heavy Israeli artillery against Kfar Rumman in southern Lebanon",
     "Israeli Air Force airstrike against the outskirts of Deir Seryan, southern Lebanon.", False,
     "SHIPPED BUG (live wire): a fresh Deir Seryan strike opened a DIFFERENT-town Kfar Rumman dot on the "
     "ONLY shared word 'against' + same region — new area = new dot (_STRIKE_GENERIC)."),
    ("Ukrainian strike drones hit an Ozon logistics hub in Chapayevsk, Samara region",
     "Putin threatened further attacks on Ukraine, saying attempts to disrupt Russian logistics would be met "
     "with strikes on Ukrainian infrastructure. You will get a response.", False,
     "SHIPPED BUG (live wire): a Putin STATEMENT about disrupting 'logistics'/'infrastructure' opened a strike "
     "ON a 'logistics hub' on that one shared word — conflict-infrastructure filler is not a subject."),
    ("Israeli strike on Deir Seryan, southern Lebanon",
     "Israeli Air Force airstrike against the outskirts of Deir Seryan, southern Lebanon.", True,
     "...but the SAME Deir Seryan strike, differently worded, must still attach on {deir, seryan}."),
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
    # A SHARED TOPIC + SHARED PLACE IS NOT THE SAME STORY. Three different Strait-of-Hormuz oil stories all
    # share {oil, shipping, cargo, trade} and the Strait, but are distinct events — topic-generic words and
    # the place-name must not attach one's media to another.
    ("Iraq's oil marketing company said Abu Dhabi's main energy company was among firms buying its crude and sending cargoes out through the Strait of Hormuz.",
     "Chinese state-owned shipping giants COSCO and CMES have stopped sending oil tankers through the Strait of Hormuz and Bab al-Mandeb due to security risks, Reuters reports.", False,
     "SHIPPED BUG: an 'Iraq sells crude via Hormuz' dot pulled a 'China avoids Hormuz' clip — shared only {oil, shipping} + the Strait"),
    ("Iraq's oil marketing company said Abu Dhabi's main energy company was among firms buying its crude and sending cargoes out through the Strait of Hormuz.",
     "U.S. officials say Oman is making progress in talks with Iran to allow more commercial shipping through the Strait of Hormuz, easing pressure on global energy markets, WSJ reports.", False,
     "the Oman/US nuclear-talks clip is a different story from the Iraq crude sale — same topic + place is not the same event"),
    ("Chinese shipping giants COSCO and CMES stop sending tankers through the Strait of Hormuz over security fears",
     "Footage: a COSCO tanker reroutes around the Cape as CMES halts Strait of Hormuz transits.", True,
     "the SAME story (COSCO/CMES pulling out) shares the distinctive company names beyond the place and must attach"),
    # A SHARED PLACE + a GENERIC diplomatic verb ("visit"/"arrive") is not the same event. SHIPPED BUG: a
    # "Lukashenko arrived in Moscow for a visit" photo was filed under a "CIA director visited Russia to warn
    # NATO" dot — they share only Moscow + "visit", two totally different subjects (Lukashenko vs Ratcliffe).
    ("reports CIA Director John Ratcliffe visited Russia to warn them not to attack NATO countries",
     "Lukashenko arrived in Moscow for an unexpected visit", False,
     "SHIPPED BUG: a Lukashenko-in-Moscow photo attached to a Ratcliffe-visits-Russia dot on {Moscow, visit} — a shared place + generic 'visit' is not a shared subject"),
    ("Lukashenko arrives in Moscow for talks with Putin",
     "Lukashenko arrived in Moscow for an unexpected visit", True,
     "...but a lone DISTINCTIVE name (Lukashenko) at the SAME place IS the same event, even though 'visit'/'arrive' are now generic"),
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
# A routine international result is true but not world news; a final/title/medal/record IS. (title, keep?)
# A climate/weather dot must be an EXTREME event, not a forecast or a rained-off match. (title, worthy?)
CLIMATE_WORTHY_CASES = [
    ("Cyclists run for cover as hailstorm forces La Vuelta stage cancellation", False),
    ("Rain delays the third Test match in Manchester", False),
    ("Storm cancels the season opener race", False),
    ("Sunny weather expected across the region this weekend", False),
    ("Hurricane makes landfall in Florida, thousands evacuated", True),
    ("Earthquake kills 200 in Afghanistan", True),
    ("Severe flooding submerges dozens of villages in Pakistan", True),
    ("Record heatwave grips southern Europe as wildfires spread", True),
]

SPORTS_WORTHY_CASES = [
    ("Turkey beat Lithuania in Women's volleyball", False),
    ("Real Madrid beat Barcelona 2-1", False),
    ("Man City sign midfielder in transfer deadline deal", False),
    ("England beat Norway to reach the World Cup semis", True),
    ("France win the World Cup final", True),
    ("Djokovic wins Wimbledon title", True),
    ("Simone Biles takes Olympic gold medal", True),
    ("Kenya sets new world record in the marathon", True),
]

# A leader card should carry IMPORTANT quotes, not personal small talk. (quote, is_important?)
QUOTE_IMPORTANT_CASES = [
    ("I Know Rumen Radev Well", False),          # SHIPPED: this led a leader card — tells a reader nothing
    ("Thank you all for the warm welcome", False),
    ("I like the idea", False),
    ("We will retaliate against any attack on our territory", True),
    ("Russia must withdraw all its troops immediately", True),
    ("The ceasefire agreement will hold despite the provocations", True),
]

FLUFF_CASES = [
    ("New Scholarships. New Programs. Your Next Step.", "https://toi.li/5Jd4WB", True,
     "SHIPPED BUG: sponsored content sat on the map as an Israel dot — an advert has no event"),
    # CULTURE / ENTERTAINMENT features are not world-news dots. SHIPPED BUG: a NYT podcast feature was a UK dot.
    ("How Two British Historians Made a Smash Hit Podcast", "", True,
     "a podcast culture feature is the arts desk, not a located world-news event"),
    ("Marvel blockbuster smashes box office records worldwide", "", True, "entertainment, not a map event"),
    ("Celebrity chef's new memoir goes viral", "", True, "celebrity/culture human-interest"),
    ("Ukraine downs 97 of 127 Russian drones overnight", "", False,
     "GUARD: real hard news must NEVER be dropped by the culture filter"),
    ("Historians uncover mass grave from the civil war", "", False,
     "GUARD: 'historians' is not automatically culture fluff — a real discovery stays"),
    # A PRE-EVENT talk announcement is not a dot: nothing has happened, people will TALK later.
    ("Sanwo-Olu, Lai Mohammed, Gbenga Daniel to discuss 2027 elections, insecurity at 7th Freedom Online lecture",
     "", True, "SHIPPED BUG: a 'to discuss at a lecture' notice was a dot"),
    ("Analysts to speak on the economy at a fintech webinar", "", True, "a webinar talk preview"),
    ("Trump and Putin to meet at Alaska summit on Ukraine", "", False,
     "GUARD: a summit is a real event — 'to meet at summit' is NOT filtered"),
    ("Officials to speak at press conference on the overnight strike", "", False,
     "GUARD: a press conference is news, not a lecture"),
    # An ART / PHOTO exhibition is the culture desk, not a world-news dot.
    ("Denis Rouvre in Salvador: 43 Free Photographs on Climate", "", True,
     "SHIPPED BUG: a photo exhibition at a cultural centre was a dot"),
    ("National gallery opens major retrospective of the painter", "", True, "an art retrospective is not news"),
    ("Satellite photographs show Russian troop buildup near the border", "", False,
     "GUARD: 'photographs' in a real intel story is not an exhibition"),
    # A NEWSLETTER DIGEST joins unrelated stories (". And,/Also,/Meanwhile,") — not one event; it also
    # mis-pairs wire clips (a clip about one half matched the whole digest dot).
    ("Trump declares economic warfare on Iran. And, SCOTUS to rule on White House ballroom", "https://t.me/x/9", True,
     "NPR 'Up First' digest of two stories in one headline -> not a single map dot"),
    ("Israel strikes Gaza, and civilians flee the north", "https://t.me/x/10", False,
     "a plain 'and' clause is NOT a digest — one real event, kept"),
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
    # A MEDIA-PERSONALITY CAREER RETROSPECTIVE is entertainment/human-interest, not a located event.
    ("Two ABC presenters share stories of 35 years in media", "", True,
     "SHIPPED BUG: a low-level media-personality career feature was a dot"),
    ("Veteran anchor marks 40 years in broadcasting", "", True, "a career-milestone feature, not an event"),
    ("Radio hosts swap memories of their careers on air", "", True, "presenters reminiscing is not news"),
    ("CNN anchor resigns amid plagiarism scandal", "", False,
     "GUARD: a presenter in a REAL event (resignation) is NOT filtered"),
    ("20 years of war in Afghanistan leave lasting scars", "", False,
     "GUARD: 'N years' about a real subject (war), not a media career, stays"),
    # A CEREMONIAL / HOLIDAY EXHORTATION (a governor's festival sermon) is filler, not a located event.
    ("Eid-el-Maulud: Abiodun urges Muslims to embrace Prophet's teachings of peace, compassion", "", True,
     "SHIPPED BUG: a governor's Eid greeting/sermon made the map"),
    ("Governor felicitates with Christians on Christmas", "", True, "a festival greeting is not news"),
    ("Cleric urges worshippers to shun violence and embrace peace", "", True, "a sermon platitude, not an event"),
    # A PROCEDURAL court/inquiry step or a witness-TESTIMONY quote — nothing decided, not country-changing.
    ("Instagram head Mosseri, at children's addiction trial, says few teenagers knew of safety feature", "", True,
     "SHIPPED BUG: a witness-testimony detail at a trial made the map"),
    ("'The people have spoken': Final hearing day of antisemitism royal commission", "", True,
     "SHIPPED BUG: a procedural 'final hearing day' made the map"),
    ("Public inquiry hearings begin into building collapse", "", True, "a hearing schedule is not an outcome"),
    ("Court sentences drug lord to life in prison", "", False, "GUARD: a VERDICT/sentence is real news"),
    ("Trump found guilty on all 34 counts", "", False, "GUARD: a conviction is real news"),
    ("Supreme Court hears arguments on abortion rights", "", False,
     "GUARD: a major court 'hears arguments' is not the procedural-hearing fluff we drop"),
    # A listicle-DIGEST roundup + subscribe plug, and corporate-PR/certification puffery, are not news.
    ("'Smart' diabetes probiotic; Chinese missile AI with 90% accuracy: 7 science highlights", "", True,
     "SHIPPED BUG: a '7 science highlights' subscribe-to-read roundup (many stories in one) made the map"),
    ("NagaWorld Earns Great Place To Work Certification in 2026 with an Outstanding 97% Trust Index Score", "", True,
     "SHIPPED BUG: a corporate certification press release (advertising) made the map"),
    ("Company named a Top Employer for 2026", "", True, "an employer-ranking PR is advertising, not news"),
    ("Subscribe to read our full coverage of the war", "", True, "a paywall/subscribe plug is not a story"),
    ("Ukraine wins EU membership bid", "", False, "GUARD: a real 'wins <thing>' political event stays"),
    ("Brain tech push gains pace as China draws up standards and firms challenge Neuralink", "", False,
     "GUARD: a real tech-policy story is not a listicle/PR"),
    # RESEARCH ASPIRATION (a study that WILL look into something) and a local professional's suspension.
    ("Most people don't survive brain cancer. Researchers hope to change", "", True,
     "SHIPPED BUG: a 'researchers hope to' project (no result yet) made the map"),
    ("Adelaide project investigating ways to treat cancer", "", True, "a study that WILL research is not an event"),
    ("'Bohemian' Melbourne schoolteacher Faye Berryman suspended", "", True,
     "SHIPPED BUG: a local teacher's registration suspension is local regulatory news"),
    ("Study finds new drug halts brain cancer", "", False, "GUARD: a real FINDING is news"),
    ("Scientists discover cancer breakthrough", "", False, "GUARD: a discovery is news"),
    ("Minister suspended over corruption scandal", "", False, "GUARD: a minister (not a schoolteacher) stays"),
    # OPINION / op-ed — a "lesson in" headline or a first-person essay body is never a located event.
    ("Syrian Kurds offer the region a lesson in nationhood", "", True,
     "SHIPPED BUG: a Middle East Monitor op-ed made the map"),
    ("A lesson in resilience from Gaza's rebuilders", "", True, "'a lesson in' is an op-ed shape"),
    ("The case for a two-state solution", "", True, "'The case for/against' opens an op-ed"),
    ("Ukraine offers Russia a ceasefire in the east", "", False, "GUARD: 'offers a ceasefire' is a real event, not 'a lesson'"),
    ("Prosecutors present the case for conviction", "", False, "GUARD: 'the case for' mid-sentence (a trial) stays"),
    ("UN urges immediate ceasefire in Gaza", "", False, "GUARD: a concrete political demand is real news"),
    ("Zelensky urges allies to send more air defense", "", False, "GUARD: 'urges allies to send' is not ceremonial"),
    ("Christmas market attack kills 5 in Germany", "", False, "GUARD: a holiday word in a real attack stays"),
    ("Trump urges Ukraine to accept peace deal", "", False, "GUARD: a diplomatic push, not a sermon"),
    # A FIRST-PERSON PERSONAL ESSAY / MEMOIR is a lived-experience column, not a located event.
    ("I was too young to understand infertility when diagnosed at 16", "", True,
     "SHIPPED BUG: a first-person personal-health memoir was a dot on the world map"),
    ("I survived the earthquake but lost my whole family", "", True, "a first-person survival memoir is a feature"),
    ("How I escaped the war in Syria", "", True, "a 'how I…' personal narrative is the features desk"),
    ("India was hit by record monsoon flooding", "", False,
     "GUARD: 'India' starts with 'I' but is not a first-person 'I' — a real disaster stays"),
    ("\"I was threatened,\" says witness in the murder trial", "", False,
     "GUARD: a reported quote opens with a quotation mark, so ^I doesn't catch it — real news stays"),
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
    # THE CONGOS: a DRC story must fly ONE DRC flag — not the DRC twice + a spurious Republic-of-Congo flag.
    ("Situation in eastern Democratic Republic of Congo, DRC: M23-allied groups expand south",
     "Dem. Rep. Congo", ["Dem. Rep. Congo"],
     "SHIPPED BUG: three Congo flags (DRC x2 + Republic) — the duplicate DRC names and bare 'Congo' collapse to one"),
    ("Fighting in the Democratic Republic of the Congo as M23 advances", "Dem. Rep. Congo", ["Dem. Rep. Congo"],
     "the 'the' spelling must NOT also fly 'Republic of the Congo' (a substring of the DRC's name)"),
    ("DRC and Republic of the Congo sign a border deal", "Dem. Rep. Congo",
     ["Dem. Rep. Congo", "Republic of the Congo"], "GUARD: a GENUINE two-Congo story keeps both"),
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
      _ev("2 missiles on course for Odesa/Chornomorsk", "security", "Odesa, Ukraine", "Ukraine", 0.6),
      _ev("Explosions in Odesa Port", "security", "Odesa, Ukraine", "Ukraine", 0.7)], 1, "security",
     "one barrage arrived as 3 terse SECURITY posts sharing only the place -> collapse to one dot"),
    ([_ev("Russia strike A", "security", "Russia", "Russia", 1.0),
      _ev("Russia strike B", "security", "Russia", "Russia", 2.0)], 2, None,
     "GUARD: a bare COUNTRY is not a specific place — two Russia stories are different events"),
    ([_ev("Gaza strike at dawn", "security", "Gaza, Palestine", "Palestine", 1.0),
      _ev("Gaza aid talks at night", "politics", "Gaza, Palestine", "Palestine", 10.0)], 2, None,
     "GUARD: same place but 9h apart — different incidents, both kept"),
    # SHIPPED BUG (the WORST over-merge): leader statements now dot the capital, so ONE Kyiv drone-defence dot
    # (security) VACUUMED every co-located "Zelensky says…" statement into a 25-source mega-dot whose text ran
    # drones -> Flag Day -> Netanyahu. A physical event must NOT absorb co-located STATEMENTS; both dots must be
    # physical to collapse. An aid-deal signing and a missile strike are DIFFERENT events -> both kept.
    ([_ev("Kyiv aid deal signed", "politics", "Kyiv, Ukraine", "Ukraine", 1.0),
      _ev("Kyiv hit by missile strike", "security", "Kyiv, Ukraine", "Ukraine", 2.0)], 2, None,
     "a statement (politics) and a strike (security) at the same capital are different events — both kept"),
    # A CAPITAL is a SEAT, never blind-merged: the leader-statement upgrade dots many unrelated stories there.
    ([_ev("Ukraine downs 97 of 127 drones overnight", "security", "Kyiv, Ukraine", "Ukraine", 0.3),
      _ev("Second wave of drones strikes Kyiv district", "security", "Kyiv, Ukraine", "Ukraine", 0.6),
      _ev("Zelensky says Ukraine reached a deal with Germany", "politics", "Kyiv, Ukraine", "Ukraine", 1.0),
      _ev("Zelensky warns elections would destroy Ukraine", "politics", "Kyiv, Ukraine", "Ukraine", 1.5),
      _ev("Ukraine marks State Flag Day", "society", "Kyiv, Ukraine", "Ukraine", 2.0)], 5, None,
     "SHIPPED BUG: the drone dot swallowed Kyiv statements — a capital is a SEAT, so nothing blind-merges there"),
    # SHIPPED BUG (the newest mega-dot): a Hasan-Piker culture story misclassified as security, a SOUTHCOM
    # airstrike and a drug-boat strike all sat on Washington (the leader-statement upgrade + being the seat)
    # and the same-category collapse folded the three UNRELATED security stories into one. A capital never
    # blind-merges — only shared CONTENT (in _merge_same_event) may fold two dots at a seat.
    ([_ev("Hasan Piker mocks Charlie Kirk over assassination tribute song", "security", "Washington, D.C.", "United States of America", 0.1),
      _ev("US Southern Command airstrike destroys cartel vessel in Eastern Pacific", "security", "Washington, D.C.", "United States of America", 0.5),
      _ev("US military strike on alleged drug-smuggling boat kills two", "security", "Washington, D.C.", "United States of America", 2.0),
      _ev("Trump threatens 50% tariffs on Canadian cars", "economy", "Washington, D.C.", "United States of America", 1.0)], 4, None,
     "three DIFFERENT security stories at a capital + an economy one -> all four stay separate"),
]

# the Live Wire must drop an admin's PERSONAL messages (greetings, sign-offs) but keep real news,
# even speculative firehose news. (text, should_drop, why)
CHATTER_CASES = [
    ("Good night, sleep well and see you all tomorrow!", True,
     "SHIPPED BUG: an admin sign-off showed on the wire as if it were a news post"),
    # SHIPPED BUG: an admin's GAMBLING tip (a bet slip) showed on the wire as if it were news.
    ("La Liga time, my money is on Betis for this one. Valencia is in the mud. Do you guys agree?", True,
     "a personal betting tip is not news"),
    ("Best bets for tonight: parlay of the day inside, cash out early", True, "gambling promo"),
    ("Place your bets on Rainbet before kickoff", True, "a betting-platform plug"),
    ("High-stakes ceasefire talks resume in Geneva", False,
     "GUARD: 'high-stakes' is not gambling — a real story stays"),
    ("Analysts weigh the odds of a Russian winter offensive", False,
     "GUARD: 'the odds of' is idiom, not a bet slip"),
    ("Thanks for following today, back tomorrow morning", True, "a thank-you + sign-off"),
    ("That's all for today, stay safe everyone", True, "a wrap-up greeting"),
    ("Subscribe to our backup channel for more", True, "channel self-promotion"),
    ("Genuinely just wanted to see a blue whale breaching the water", True,
     "SHIPPED: an admin's personal off-topic aside showed on the wire as if it were news"),
    ("Off-topic but check out this sunset over the harbour", True, "a personal off-topic aside"),
    ("I really thought Trump would give up on the Iran war by now. Fair enough tbh", True,
     "SHIPPED: an admin's casual first-person OPINION ('tbh', 'fair enough') is editorialising, not news"),
    ("Trump says he will meet Putin, adding I think we can make a deal", False,
     "GUARD: a quoted 'I think' inside a real report is NOT admin chatter"),
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
    # SHIPPED BUG: an admin's OPINION became a map dot. _tg_reliable screened self-promo but not first-person
    # editorialising, so "In my opinion, this is a very poor decision…" got a dot. It must be dropped — while
    # attributed statements above stay.
    ("In my opinion, this is a very poor decision by the U.S. armed forces and reflects an inability to evolve",
     False, "an admin's first-person OPINION is never a map dot (editorialising, not reporting)"),
    ("Personally, I think this whole escalation is being blown way out of proportion", False,
     "'personally, I think' = the admin's take, not an event"),
    # SHIPPED BUG: a channel's META note ABOUT the wire — debunking recycled footage and explaining its own
    # coverage — got through as news. It provides no event; drop it. Real news that merely mentions video/
    # recycling (with no debunk framing) must still pass, so the guards below stay True.
    ("Ansarullah is recycling 2-6 year old clips of attacks on Saudi positions, republishing them as new; "
     "hence Rerum Novarum's lack of coverage of the clips. Note, there are no Abrams tanks in western Yemen.",
     False, "channel meta: debunking recycled clips + explaining its own coverage is housekeeping, not news"),
    ("Norway now recycles 97 percent of its plastic bottles, environment ministry says", True,
     "GUARD: a real 'recycles' story with no media noun nearby is not a footage debunk"),
    ("Video shows the aftermath of the Israeli strike on the southern suburbs", True,
     "GUARD: plain footage of an event (no 'old/fake/recycled' debunk word) is real news"),
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
    ("Fire broke out at Rosneft's Komsomolsk-on-Amur refinery in Khabarovsk Krai, no casualties reported",
     True, "a strike/fire on STRATEGIC infrastructure (refinery) reaches the map even with no casualties and "
     "even when the target downplays it — the war-and-security news the map exists for"),
    ("Ukrainian drone strike sets a fuel depot ablaze in Rostov region", True, "attack + depot = strategic hit"),
    ("Explosion knocks out a power plant, cutting electricity to the region", True, "blast + power plant"),
    ("Illegal sand extraction erodes Cape Verde coastline", False, "minor/local -> defer to the AI SCOPE"),
    ("Local bakery in Lisbon revives a traditional recipe", False, "human-interest -> defer to the AI SCOPE"),
    ("Wildfire spreads across the Australian outback", False, "a fire with NO strategic facility is not forced on"),
    ("City council debates a new port-city housing development", False, "'port city' is not a struck facility"),
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
    # A wire post truncated mid-clause must never end a headline on "…" or a dangling connector.
    ("UK PM to visit Kyiv to help improve its production of long-range missiles and.",
     "!and", "SHIPPED BUG: the headline ended on 'missiles and.' — drop the dangling conjunction"),
    ("UK PM to visit Kyiv to help improve its production of long-range missiles and.",
     "long-range missiles", "…and the real content survives"),
    ("Iran warns ships in Strait of Hormuz of fines, detention or...", "detention",
     "'detention or...' -> the ellipsis and dangling 'or' are trimmed"),
    ("What Iran is really capable of", "capable of",
     "GUARD: a legit headline ending in a stranded preposition is NEVER trimmed"),
    ("Russia and Ukraine agree to a ceasefire", "Russia and Ukraine",
     "GUARD: a mid-sentence 'and' is never touched"),
    # A bare short-link stapled to a headline ("… says video bit.ly/4qyMxQB") is furniture, not news.
    ("New Colombia president orders migrant deportations says video bit.ly/4qyMxQB", "!bit.ly",
     "SHIPPED BUG: a bare bit.ly link stayed in the headline"),
    ("New Colombia president orders migrant deportations says video bit.ly/4qyMxQB", "!video",
     "...and the dangling 'says video' callout is dropped with it"),
    ("New Colombia president orders migrant deportations says video bit.ly/4qyMxQB", "migrant deportations",
     "...leaving the actual news intact"),
    # A long statement CHAR-truncated to ~200 must not end on a dangling connector. SHIPPED BUG (again): a
    # Katz statement was cut to "…launching of kites and." — the length cut created a fresh dangling "and".
    ("Israel's Minister of Defense, Israel Katz says Prime Minister Netanyahu and I have instructed the IDF to "
     "adopt a policy of zero tolerance and zero containment toward the launching of kites and balloons from Gaza",
     "!kites and", "the truncation must not leave a dangling 'kites and.'"),
    ("Israel's Minister of Defense, Israel Katz says Prime Minister Netanyahu and I have instructed the IDF to "
     "adopt a policy of zero tolerance and zero containment toward the launching of kites and balloons from Gaza",
     "launching of kites", "...it ends cleanly on the content word"),
    ("Massive fire hits refinery in Rostov region - BBC News",
     "!BBC", "a multi-word outlet ('BBC News') byline is still stripped"),
    # SOURCE / SPEAKER ATTRIBUTION is CONTENT, not a byline — it must SURVIVE. SHIPPED BUG: TASS's
    # "…positions, militants — platoon commander" lost who the claim was sourced to, reading oddly.
    ("Battlegroup North artillery destroys Kiev forces positions, militants — platoon commander",
     "platoon commander", "a '— <role>' source attribution is part of the news and must be kept"),
    ("Ukraine downs 40 Russian drones overnight — Zelensky",
     "Zelensky", "a '— <speaker>' attribution (who said it) must be kept"),
    ("Air defenses repelled the attack on the capital — defense ministry",
     "ministry", "a '— <institution>' attribution must be kept"),
    ("Battlegroup North artillery destroys Kiev forces positions,militants — platoon commander",
     "positions, militants", "a missing space after a comma between two words is repaired"),
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
    # A media word is only a callout to strip when it is SET OFF (a separator or a 'watch/says' cue), never a
    # bare noun ending a real headline. SHIPPED BUG: "…Over Anti-Iran Video" lost "Video" (the video IS the story).
    ("Iraqi Teen Decapitated in Baghdad Over Anti-Iran Video", "Anti-Iran Video",
     "a meaningful trailing 'Video' (the video the teen posted) must NOT be stripped as a media callout"),
    ("Massive explosion rocks Beirut port - video", "!video",
     "a genuine '— video' callout (set off by a dash) IS still stripped"),
    # Inline emoji copied from a Telegram post must never reach a headline.
    ("\U0001f525 Massive explosion rocks Beirut port", "Massive explosion rocks Beirut port",
     "a leading fire emoji is stripped from the headline"),
    ("Protesters clash with police ➡️ dozens detained", "Protesters clash with police dozens detained",
     "an inline arrow emoji is stripped from the headline"),
    # WIRE ABBREVIATIONS -> plain words (our house style), and trademark/replacement-char junk stripped.
    ("Novatek to pay dividends for 1H amounting to 35.5 rubles per share", "first half",
     "'1H' spelled out to 'first half'"),
    ("Payout could total 107.79 bln rubles", "billion", "'bln' spelled out to 'billion'"),
    ("NagaWorld Earns Great Place To Work� Certification™ in 2026", "!™",
     "trademark glyph stripped from the headline"),
    ("NagaWorld Earns Great Place To Work� Certification™ in 2026", "!�",
     "the U+FFFD replacement char (broken encoding) stripped"),
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

    print("\n=== SPORTS WORTHINESS (only MAJOR results reach the map) ===")
    for title, keep in SPORTS_WORTHY_CASES:
        got = app._sports_worthy(title)
        ok = got == keep
        ran[0] += 1
        if not ok:
            fails.append(("sports", title, keep, got, "major result -> keep; routine result -> drop"))
        print(f"  {'ok ' if ok else 'FAIL'} {'KEEP' if got else 'DROP'} (want {'KEEP' if keep else 'DROP'}) {title[:44]}")

    print("\n=== CLIMATE WORTHINESS (only EXTREME weather reaches the map) ===")
    for title, keep in CLIMATE_WORTHY_CASES:
        got = app._climate_worthy(title)
        ok = got == keep
        ran[0] += 1
        if not ok:
            fails.append(("climate", title, keep, got, "extreme weather -> keep; forecast/rained-off match -> drop"))
        print(f"  {'ok ' if ok else 'FAIL'} {'KEEP' if got else 'DROP'} (want {'KEEP' if keep else 'DROP'}) {title[:44]}")

    print("\n=== QUOTE IMPORTANCE (leader cards carry substance, not small talk) ===")
    for q, keep in QUOTE_IMPORTANT_CASES:
        got = app._quote_important(q)
        ok = got == keep
        ran[0] += 1
        if not ok:
            fails.append(("quote", q, keep, got, "important statement -> keep; personal small talk -> drop"))
        print(f"  {'ok ' if ok else 'FAIL'} {'KEEP' if got else 'DROP'} (want {'KEEP' if keep else 'DROP'}) {q[:44]}")

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

    # NAME-BASED DEDUP: the same story reworded shares almost no ordinary words but the same NAMES. The dot's
    # dedup merges when 3+ proper names overlap (+ same place/day); distinct stories share only the countries.
    print("\n=== NAME DEDUP (re-headlined copy caught by shared names) ===")
    def _props(t): return {w.rstrip("'") for w in app._proper_words(t)}
    _dupA = _props("Israel is astonished at Iran's rapid military rebuild. The Jerusalem Post reports Tel Aviv believes Iran will rebuild by 2028.")
    _dupB = _props("The Jerusalem Post, citing IDF and Mossad, reports Israel taken by surprise by Iran's speedy defense recovery after the war.")
    _difA = _props("Israel strikes Natanz nuclear site in Iran overnight")
    _difB = _props("Iran's Khamenei vows revenge on Israel after a Tehran blast")
    _name_ok = (len(_dupA & _dupB) >= 3 and len(_difA & _difB) < 3)
    ran[0] += 1
    if not _name_ok:
        fails.append(("name-dedup", "reworded copy", "dup>=3 names, different<3",
                      f"dup={len(_dupA & _dupB)} diff={len(_difA & _difB)}",
                      "a re-headlined copy must be caught by shared NAMES while distinct stories are not"))
    print(f"  {'ok ' if _name_ok else 'FAIL'} dup shared names={len(_dupA & _dupB)}, different={len(_difA & _difB)}")

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
    _oe, _os, _omf, _ohf = app._wd_entities, app._wd_search_person, app._minister_for, app._heads_for
    import os as _os_mod
    try:
        _os_mod.remove(_os_mod.path.join(app.CACHE_DIR, "leaders_Q99999901.json"))   # deterministic: no stale cache
    except Exception:
        pass
    try:
        app._wd_entities = lambda *a, **k: {}          # simulate HTTP 429 — Wikidata gives nothing
        app._wd_search_person = lambda *a, **k: None
        app._minister_for = lambda *a, **k: None       # isolate to head-of-state/gov (cabinet is a Wikipedia source, unaffected by a Wikidata 429)
        app._heads_for = lambda *a, **k: None          # and isolate from the Wikipedia heads list, so this tests the Factbook fallback proper
        _r = app.Api().country_leaders("Q99999901", "Saudi Arabia", _sa_fb)
    finally:
        app._wd_entities, app._wd_search_person, app._minister_for, app._heads_for = _oe, _os, _omf, _ohf
    _names = [L.get("name", "") for L in _r.get("leaders", [])]
    _clean = len(_names) == 2 and all(_names) and not any("crown salman" in n.lower() for n in _names)
    ran[0] += 1
    if not _clean:
        fails.append(("leaders-429", "Saudi Arabia", "King + MBS, clean names", str(_names),
                      "a rate-limited fetch must fall back to clean Factbook names, keeping a distinct head of government"))
    print(f"  {'ok ' if _clean else 'FAIL'} rate-limited Saudi -> {_names}")

    # AUTHORITATIVE OVERRIDE: Wikidata P6 and the Factbook both lag a reshuffle, so when the daily 'current
    # heads of state and government' list names a DIFFERENT current holder, country_leaders must trust the list
    # (the real bug: the UK PM stuck on Keir Starmer when Andy Burnham had taken over).
    print("\n=== HEADS LIST OVERRIDES A STALE HEAD OF GOVERNMENT ===")
    _oe2, _os2, _omf2, _ohf2, _oimg = (app._wd_entities, app._wd_search_person,
                                       app._minister_for, app._heads_for, app._wiki_person_img)
    try:
        _os_mod.remove(_os_mod.path.join(app.CACHE_DIR, "leaders_Q99999902.json"))
    except Exception:
        pass
    try:
        app._wd_entities = lambda *a, **k: {}                  # no Wikidata -> heads come from the Factbook...
        app._wd_search_person = lambda *a, **k: None
        app._minister_for = lambda *a, **k: None
        app._wiki_person_img = lambda *a, **k: "http://img/x.jpg"
        app._heads_for = lambda c: {"hos": {"name": "Charles III", "title": "King", "article": "Charles III"},
                                    "hog": {"name": "Andy Burnham", "title": "Prime Minister", "article": "Andy Burnham"}}
        _uk = app.Api().country_leaders("Q99999902", "United Kingdom",
                                        {"cos": "King CHARLES III",
                                         "hog": "Prime Minister Keir STARMER (since 5 July 2024)"})
    finally:
        (app._wd_entities, app._wd_search_person, app._minister_for,
         app._heads_for, app._wiki_person_img) = _oe2, _os2, _omf2, _ohf2, _oimg
    _uk_names = {L.get("title", ""): L.get("name", "") for L in _uk.get("leaders", [])}
    _ok_uk = (_uk_names.get("Prime Minister") == "Andy Burnham"
              and "Keir Starmer" not in _uk_names.values())
    ran[0] += 1
    if not _ok_uk:
        fails.append(("heads-override", "United Kingdom", "PM=Andy Burnham (not Starmer)", str(_uk_names),
                      "the daily heads-of-state list must override a stale Wikidata/Factbook head of government"))
    print(f"  {'ok ' if _ok_uk else 'FAIL'} stale Starmer -> {_uk_names.get('Prime Minister')}")

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
        # SOFT NEWS never reaches the WORLD map (only a starred country's own feed): a kangaroo drowning or
        # stranded holidaymakers are dropped even with a real scene+scope, while a war-reparations demand at the
        # UN — which merely shares the word "compensation" — is kept. (The starred-feed path never calls this.)
        app._ai_scope = lambda t: "regional"; app._ai_where = lambda t: "Sydney, Australia"
        _mw_soft = app._map_worthy("Kangaroo drownings in a canal spark community concern", "",
                                   (-33.8, 151.2, "Sydney, Australia", "Australia"))
        _mw_strand = app._map_worthy("Qantas passengers stranded in Johannesburg for days demand compensation", "",
                                     (-26.2, 28.0, "Johannesburg, South Africa", "South Africa"))
    finally:
        app._ai_scope, app._ai_where = _o_sc, _o_w
    _soft_precise = (app._soft_news("Kangaroo drownings spark community concern", "")
                     and app._soft_news("Qantas passengers stranded demand refunds", "")
                     and not app._soft_news("Ukraine demands compensation for Russian war damage at UN", "")
                     and not app._soft_news("Israel strikes Hezbollah positions, 12 killed", "")
                     # ONE person's personal journey is not region-changing; aggregate conflict news IS kept.
                     and app._soft_news("Palestinian American returns to West Bank to defend his home under siege by Israeli settlers", "")
                     and app._soft_news("Meet the family who refused to leave their village", "")
                     and not app._soft_news("Israeli settlers attack a Palestinian village in the West Bank", "")
                     and not app._soft_news("Zelensky returns to Kyiv to lead the war effort", "")
                     # LOCAL LIFESTYLE — a festival/concert is not world news; a deadly stampede at one IS.
                     and app._soft_news("Free Beer Festival Hits Belo Horizonte Saturday With 30 Local Brews", "")
                     and app._soft_news("Rio jazz festival returns this weekend", "")
                     and not app._soft_news("Stampede at a festival kills 10", "")
                     # a CULTURAL ART TOUR / street-art / exhibition event is a lifestyle feature, off the world map.
                     and app._soft_news("Florianopolis Paints Itself Lusophone for the Third Street Art Tour",
                                        "The third Festival Street Art Tour runs September 1-7 with 100+ artists.")
                     and app._soft_news("Venice Biennale art exhibition opens to crowds", "")
                     and not app._soft_news("Thieves steal Van Gogh painting from museum in overnight art heist", "")
                     and not app._soft_news("Defense expo showcases new fighter jets in Ankara", "")
                     # a LONE ACCIDENTAL death is local; a mass toll or a violent death stays on the map.
                     and app._soft_news("Electricity worker dies in electrocution incident", "")
                     and app._soft_news("Driver killed in road accident", "")
                     and not app._soft_news("20 die in a road accident in Nigeria", "")
                     and not app._soft_news("Soldiers killed in a Russian strike", "")
                     # professional-misconduct / celebrity-legal is local human-interest; a real crime is kept
                     and app._soft_news("Doctor to the stars cleared over failure to record reason for using labour drug", "")
                     and app._soft_news("Surgeon struck off after professional misconduct hearing", "")
                     and not app._soft_news("Doctor charged with murder of five patients", "")
                     # a lone citizen's death abroad handled as a consular case is local; a mass event is kept
                     and app._soft_news("Australian dies in Vietnam", "DFAT confirms it is providing consular assistance to the family")
                     and not app._soft_news("Nine killed in a Kolkata hotel fire", "")
                     # NEAR-MISS is not news (nothing happened); a real strike/casualty stays on the map.
                     and app._soft_news("Aussie star almost hit by car in Tour of Britain", "her close call with a car while riding")
                     and app._soft_news("Cyclist narrowly avoided a truck on the motorway", "")
                     and not app._soft_news("Air strike hits a hospital, killing 3", "")
                     # EXPLAINER/service features pose a question and report no event -> off the map.
                     and app._soft_news("What a cancer survivor's legal win means for workers returning after illness", "Lawyers and HR practitioners weigh in.")
                     and app._soft_news("Here's what you need to know about the new tax rules", "")
                     and not app._soft_news("Ceasefire collapses as 60 are killed in renewed strikes", "")
                     # a BLAME-DEFLECTION talking point reports no event; a real accountability finding is kept.
                     and app._soft_news("Iran was not the one to trigger escalation in Middle East", "")
                     and not app._soft_news("Inquiry finds negligence caused the ferry disaster", "")
                     # a RETROSPECTIVE/HISTORY feature is a look-back, not breaking news; a real event is kept.
                     and app._soft_news("How a world-leading health study in a small town helped shape modern medicine", "")
                     and app._soft_news("How the Berlin Wall shaped a generation", "")
                     and not app._soft_news("Missile strike kills 12 in Kyiv", ""))
    # "Georgia" the US STATE must not fly the COUNTRY's flag in a US-context story (recurring bug).
    _ga_ok = (app._involved_countries("Hyundai U.S. production increase at new Georgia plant", "United States of America") == ["United States of America"]
              and "Georgia" in app._involved_countries("Russia masses troops on the Georgia border", "Georgia"))
    _mw_ok = ((not _mw_broad) and _mw_scene and (not _mw_local) and _mw_cas and _mw_new
              and (not _mw_soft) and (not _mw_strand) and _soft_precise and _ga_ok)
    ran[0] += 1
    if not _mw_ok:
        fails.append(("map-worthy", "importance gate",
                      "broad+local+soft DROP; scene/casualty/unrated KEEP; reparations not soft",
                      f"broad={_mw_broad} scene={_mw_scene} local={_mw_local} casualty={_mw_cas} new={_mw_new} "
                      f"soft={_mw_soft} strand={_mw_strand} soft_precise={_soft_precise}",
                      "the world map hides broad features, minor-local & soft news, keeps real scenes/casualties"))
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
              and _lc.get("Iran") == "tehran" and not app._good_img(_flagurl)
              # a Google-News RSS item's url is a news.google.com REDIRECT whose og:image is the Google News
              # mark (it shipped as a Spain wildfire hero); gstatic.com is Google branding — both rejected,
              # while a real cached photo on the googleusercontent proxy is still kept.
              and not app._good_img("https://news.google.com/img/logo.png")
              and not app._good_img("https://www.gstatic.com/news/logo.svg")
              and app._good_img("https://lh3.googleusercontent.com/proxy/abc=w800")
              # a historical/topographic MAP is not a photo (a 1957 Victoria Harbour map slipped through onto a hero)
              and not app._good_img("https://upload.wikimedia.org/wikipedia/commons/thumb/6/65/1957_map_of_Victoria_Harbour.jpg/1280px-x.jpg")
              # city-states/microstates have a curated landmark query so their hero is never a rejected flag/black frame
              and app._PLACE_PHOTO_QUERY.get("singapore") and app._PLACE_PHOTO_QUERY.get("hong kong"))
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

    # SUMMARY CACHE IS INDEPENDENT OF _DATA_VER — a brief depends on the PROMPT (_SUM_PROMPT_VER) + the story
    # text, never on the feed-content version. A _DATA_VER bump must serve the CACHED brief (no re-summarize);
    # keying it by _DATA_VER used to wipe every brief on each bump and force a ~500k-token regen over Groq's
    # 200k-tokens/DAY free cap.
    print("\n=== SUMMARY CACHE (survives a _DATA_VER bump) ===")
    _orig_complete, _orig_savail, _orig_dver = app._llm_complete, app._llm_available, app._DATA_VER
    _sum_calls = [0]
    _nonce = os.urandom(6).hex()
    _before = {f for f in os.listdir(app.CACHE_DIR) if f.startswith("sum_")}
    try:
        app._llm_available = lambda: True
        def _fake_complete(system, user, **k):
            _sum_calls[0] += 1
            return "A short original brief about the nonce event."   # no WHERE/SCOPE lines to parse out
        app._llm_complete = _fake_complete
        _t = "Nonce cache headline " + _nonce
        _x = "Plain factual body text for the nonce story " + _nonce + "."
        app._summarize(_t, _x)                        # 1st call: cache miss -> generates + writes cache
        _first = _sum_calls[0]
        app._DATA_VER = "d_CHANGED_" + _nonce         # simulate a feed-content version bump
        app._summarize(_t, _x)                        # 2nd call: must hit cache -> NO new LLM call
        _second = _sum_calls[0]
    finally:
        app._llm_complete, app._llm_available, app._DATA_VER = _orig_complete, _orig_savail, _orig_dver
        for f in {f for f in os.listdir(app.CACHE_DIR) if f.startswith("sum_")} - _before:
            try: os.remove(os.path.join(app.CACHE_DIR, f))
            except Exception: pass
    _sumcache_ok = (_first == 1 and _second == 1)
    ran[0] += 1
    if not _sumcache_ok:
        fails.append(("sum-cache", "_DATA_VER independence", "generate once then cache-hit",
                      f"first={_first} second={_second}",
                      "the summary cache must NOT be keyed by _DATA_VER, or every content bump re-summarizes everything"))
    print(f"  {'ok ' if _sumcache_ok else 'FAIL'} brief generated once ({_first}), then served from cache after a _DATA_VER bump ({_second})")

    # CACHE JANITOR — the "auto-clear the waste" timer. A daily sweep reclaims disk from EXPIRED derived caches
    # (mtime past every 30-day TTL) while NEVER touching live state: the served feed (world_*), a dot's LOCATION
    # (aiwhere_*), the merge verdicts (dedup_*), or leader identity. It must reclaim disk without moving a dot.
    print("\n=== CACHE JANITOR (clears stale waste, spares live dots/data) ===")
    import tempfile as _tf, shutil as _sh, time as _tm
    _jd = _tf.mkdtemp(); _orig_cache = app.CACHE_DIR
    try:
        app.CACHE_DIR = _jd
        _old = _tm.time() - 50 * 86400; _newm = _tm.time() - 2 * 86400
        def _mkf(n, mt):
            _p = os.path.join(_jd, n); open(_p, "w").write("x"); os.utime(_p, (mt, mt))
        for _n, _mt in [("sum_dead.json", _old), ("geoai_dead.json", _old), ("media_dead.json", _old),
                        ("sum_fresh.json", _newm), ("world_24h.json", _old), ("aiwhere_x.json", _old),
                        ("dedup_x.json", _old), ("leaders_Q1.json", _old)]:
            _mkf(_n, _mt)
        app._purge_stale_cache()
        _left = set(os.listdir(_jd))
        _janitor_ok = ("sum_dead.json" not in _left and "geoai_dead.json" not in _left
                       and "media_dead.json" not in _left and "sum_fresh.json" in _left        # fresh kept
                       and "world_24h.json" in _left and "aiwhere_x.json" in _left              # live state spared
                       and "dedup_x.json" in _left and "leaders_Q1.json" in _left)
        _mkf("sum_dead2.json", _old)                 # marker guard: an immediate second sweep is a no-op
        app._purge_stale_cache()
        _guard_ok = "sum_dead2.json" in set(os.listdir(_jd))
    finally:
        app.CACHE_DIR = _orig_cache
        _sh.rmtree(_jd, ignore_errors=True)
    _jan_ok = _janitor_ok and _guard_ok
    ran[0] += 1
    if not _jan_ok:
        fails.append(("janitor", "stale-only purge", "stale derived cleared, live state spared, guarded",
                      f"janitor={_janitor_ok} guard={_guard_ok}",
                      "the auto-clear must delete only expired derived caches and never a feed/location/dedup file"))
    print(f"  {'ok ' if _jan_ok else 'FAIL'} stale sum_/geoai_/media_ cleared; world_/aiwhere_/dedup_/leaders_ spared; guarded")

    # GEMINI FAILOVER — a primary (Groq) empty return (a daily-cap 429) must roll to GEMINI, not lose the
    # answer; and a geo second opinion can PREFER Gemini (an independent model family) yet still fall back.
    print("\n=== GEMINI FAILOVER (backup provider + preferred second opinion) ===")
    _orig_one2, _orig_gk2, _orig_cfg2 = app._llm_one, app.load_gemini_key, app._summary_cfg
    _seen = []
    try:
        app._summary_cfg = lambda: ("gsk-FAKE", "https://primary/x", "primary-model")
        app.load_gemini_key = lambda: "AQ.FAKE"
        def _fake_one(name, key, url, model, system, user, mt, temp):
            _seen.append(name)
            return "" if name == "primary" else "BACKUP:" + name
        app._llm_one = _fake_one
        _failover = app._llm_complete("s", "u")                 # primary empty -> rolls to gemini
        _order = list(_seen); _seen.clear()
        app._llm_complete("s", "u", prefer="gemini")            # prefer -> gemini tried FIRST
        _pref_first = _seen[0] if _seen else None
    finally:
        app._llm_one, app.load_gemini_key, app._summary_cfg = _orig_one2, _orig_gk2, _orig_cfg2
    _cb_ok = (_failover == "BACKUP:gemini" and _order[:2] == ["primary", "gemini"] and _pref_first == "gemini")
    ran[0] += 1
    if not _cb_ok:
        fails.append(("gemini", "failover+prefer", "primary->gemini; prefer=gemini first",
                      f"failover={_failover!r} order={_order} pref_first={_pref_first!r}",
                      "a capped primary must roll to Gemini; prefer must front-load it for the geo second opinion"))
    print(f"  {'ok ' if _cb_ok else 'FAIL'} primary empty -> {_failover!r}; order={_order}; prefer-first={_pref_first!r}")

    # SOURCE MUTE — a muted outlet is hidden from the map (no dots, no citations), matched on domain / name
    # / url; other outlets are untouched. (The default mutes The Guardian per the user.)
    print("\n=== SOURCE MUTE (a hidden outlet never reaches the map) ===")
    _mute_ok = (app._is_muted("theguardian.com", "The Guardian", "https://www.theguardian.com/world/x")
                and app._is_muted("", "", "https://amp.theguardian.com/x")
                and app._is_muted("thegatewaypundit.com", "The Gateway Pundit", "https://www.thegatewaypundit.com/2026/x")
                and app._is_muted("", "The Gateway Pundit", "")
                and not app._is_muted("bbc.co.uk", "BBC", "https://bbc.co.uk/x")
                and not app._is_muted("news.antiwar.com", "Antiwar.com", ""))
    ran[0] += 1
    if not _mute_ok:
        fails.append(("mute", "guardian", "guardian hidden, others kept", str(_mute_ok),
                      "a muted source must be filtered on domain/name/url, and only that source"))
    print(f"  {'ok ' if _mute_ok else 'FAIL'} guardian+gateway-pundit muted; bbc/antiwar kept; mutes={sorted(app._muted_sources())}")

    # PORT PROFILE — the JSON extractor keeps only the expected string fields (ignoring junk / markdown fences)
    # and returns None on non-JSON, so a bad model reply degrades to "no profile" instead of crashing the popup.
    print("\n=== PORT PROFILE (json extractor) ===")
    _pj = app._port_json('```json\n{"type":"Container & transshipment","opened":"2007","throughput":"~5M TEU (2023)","ships_per_day":"~40","junk":123,"significance":"Major hub"}\n```')
    # BASELINE — every port gets real content with ZERO LLM: a curated port has a ranking + waters; an unknown
    # port still gets a type + a plain role line, so the popup never says "no profile available".
    _pb = app._port_baseline("Jebel Ali", "United Arab Emirates")
    _pb2 = app._port_baseline("Some Tiny Harbour", "Fakeland")
    _port_ok = (isinstance(_pj, dict) and _pj.get("type") == "Container & transshipment"
                and _pj.get("opened") == "2007" and "junk" not in _pj
                and set(_pj.keys()) <= {"type", "opened", "operator", "throughput", "ships_per_day", "significance", "recent", "waters"}
                and app._port_json("no json here at all") is None
                and app._port_json('{"nothing":"useful"}') is None      # no expected field -> None
                and "Middle East" in _pb.get("rank", "") and _pb.get("waters") and _pb.get("type")   # curated -> ranking + waters
                and _pb2.get("type") and _pb2.get("significance"))       # unknown -> still a usable baseline
    # INFOBOX FACTS — real basic facts (founded/type/operator/throughput/berths) parsed from a Wikipedia
    # infobox, de-wikified: {{Start date}} -> year, [[link|text]] -> text, a bare-year "throughput" dropped.
    _wt = ("{{Infobox port\n| name = Port of Testville\n| opened = {{Start date|1965}}\n"
           "| type = [[Container port]]\n| operated = [[DP World|DP World Ltd]]\n"
           "| containervolume = 13.7 million TEU (2021) up 1.9% year-on-year<ref>x</ref>\n"
           "| berths = 67\n}}\nLead paragraph text here.")
    _if = app._port_infobox_facts(_wt)
    _if_junk = app._port_infobox_facts("{{Infobox port\n| containervolume = (2025)\n| name = X\n}}")
    _if_ok = (_if.get("opened") == "1965" and _if.get("type") == "Container port"
              and _if.get("operator") == "DP World Ltd"
              and _if.get("throughput") == "13.7 million TEU (2021)"     # tail + ref trimmed
              and _if.get("berths") == "67"
              and "throughput" not in _if_junk                            # a bare year is not a throughput
              and app._infobox_map("no infobox here") == {})
    ran[0] += 1
    if not _port_ok:
        fails.append(("port", "json+baseline", "clean fields + baseline for every port", str([_pj, _pb, _pb2]),
                      "port_json keeps expected fields; _port_baseline always yields content"))
    print(f"  {'ok ' if _port_ok else 'FAIL'} port json + baseline (Jebel Ali rank='{_pb.get('rank','')[:32]}')")
    ran[0] += 1
    if not _if_ok:
        fails.append(("port", "infobox", "founded/type/operator/throughput/berths parsed + cleaned", str([_if, _if_junk]),
                      "the Wikipedia infobox parser must de-wikify basic facts and reject junk figures"))
    print(f"  {'ok ' if _if_ok else 'FAIL'} infobox facts -> {_if}")

    # FACILITY TYPE IS NOT A PLACE — a bare "airport"/"seaport"/… must never be a standalone dot. SHIPPED:
    # "Matecaña International Airport, Pereira's Airport, is now in shambles" dotted "Airport, United States"
    # (a Honolulu neighbourhood) instead of the airport's real city, Pereira, Colombia.
    print("\n=== FACILITY WORD NOT A PLACE (airport != a town) ===")
    _fa_t = "Matecana International Airport, Pereira's Airport, is now in shambles"
    _fa_m = app._context_mentions(_fa_t, "")
    _fa_hits, _fa_words = app._scan_places(_fa_t, app._person_spans(_fa_t), _fa_m)
    _fa_pick = app._pick_place(_fa_hits, _fa_words)
    # A wrong AI WHERE must not override a rule scene the HEADLINE names (the Orenburg-over-Khabarovsk bug):
    _kh_t = "Fire at Rosneft's Komsomolsk-on-Amur refinery in Khabarovsk Krai, farther than the Orsk refinery"
    _pit_ok = (app._place_in_title("Khabarovsk Krai, Russia", _kh_t)          # the rule scene IS in the headline
               and not app._place_in_title("Orenburg Region, Russia", _kh_t)  # the AI's guess is NOT
               and not app._place_in_title("", _kh_t))
    _fa_ok = ("airport" in app._NEVER_CITY_WORDS
              and not any("Airport" in (h[4] or "") for h in _fa_hits)     # the type word is not a dot
              and _fa_pick is not None and "Pereira" in (_fa_pick[4] or "")  # the real city wins
              and _pit_ok)
    ran[0] += 1
    if not _fa_ok:
        fails.append(("facility", "airport", "airport blocked; Pereira wins", str([(h[4], h[1]) for h in _fa_hits]),
                      "a bare facility-type word must never outrank the real city"))
    print(f"  {'ok ' if _fa_ok else 'FAIL'} '...International Airport, Pereira's Airport' -> {_fa_pick[4] if _fa_pick else None}")

    # FINISH THE BRIEF — a summary must never end mid-sentence. _finish_brief trims a dangling tail back to the
    # last full sentence, but leaves an already-complete brief (and a short whole one) untouched.
    print("\n=== FINISH BRIEF (no summary ends mid-sentence) ===")
    _fb_cases = [
        # (input, expected)
        ("Floods hit the valley. Thousands fled as the river",  "Floods hit the valley."),   # trim dangling tail
        ("A dam broke overnight, forcing evacuations.",         "A dam broke overnight, forcing evacuations."),  # already whole
        ('The minister said the deal was "done."',              'The minister said the deal was "done."'),  # closes on quote
        ("Talks collapsed and the",                              "Talks collapsed."),   # dangling connector -> trim + close cleanly
        ("has approved Destry Damayanti as the new governor of.", "has approved Destry Damayanti as the new governor."),  # dangling PREPOSITION before a period (the Antara BI-governor cutoff)
        ("Aid arrived Monday.\n- Scale: 3,000 homes lost\n- Cost: still being",
         "Aid arrived Monday.\n- Scale: 3,000 homes lost"),     # drop the incomplete final bullet
        # THE RECURRING CUTOFF: a brief ending in a truncation marker "…" + a dangling attribution reads as
        # finished (last char is ".") but is NOT — strip the "…, X says…" tail. This is the weeks-long bug.
        ("Explosive materials stored inside the house caused the blast, Civil Defense says...",
         "Explosive materials stored inside the house caused the blast"),
        ("The strike hit a fuel depot, Reuters reports…",
         "The strike hit a fuel depot"),
    ]
    _fb_ok = True
    for _inp, _exp in _fb_cases:
        _got = app._finish_brief(_inp)
        ran[0] += 1
        if _got != _exp:
            _fb_ok = False
            fails.append(("finish_brief", _inp[:40], _exp, _got, "a brief must end on a complete sentence"))
    print(f"  {'ok ' if _fb_ok else 'FAIL'} briefs end whole (mid-sentence tails trimmed, complete ones kept)")

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
    # DIFFERENT PLACES = DIFFERENT EVENTS: a strike on the Komsomolsk-on-Amur refinery (far-east Khabarovsk
    # Krai) must NOT fold into a strike on the Orsk refinery (Orenburg, ~6,000 km away) just because both are
    # Ukrainian strikes on a Russian refinery. (The shipped bug that hid the far-east dot for a whole day.)
    _refineries = [
        {"title": "Ukrainian drone strike hits the Orsk oil refinery in Russia", "place": "Orsk, Russia",
         "country": "Russia", "lat": 51.2, "lng": 58.6, "hrs": 5.0, "source": "NOELREPORTS",
         "cat": "security", "image": "", "sum": "Orsk refinery hit.", "url": "r1"},
        {"title": "Fire at Rosneft's Komsomolsk-on-Amur refinery in Khabarovsk Krai after Ukrainian strike",
         "place": "Khabarovsk Krai, Russia", "country": "Russia", "lat": 48.48, "lng": 135.08, "hrs": 11.0,
         "source": "NOELREPORTS", "cat": "security", "image": "", "sum": "Komsomolsk refinery fire.", "url": "r2"},
    ]
    _diff_ok = len(app._merge_same_event([dict(e) for e in _refineries])) == 2   # two distinct scenes, two dots
    # REWORDED SAME EVENT (no shared toll, no near-identical wording, event nouns stripped as generic) must
    # STILL merge deterministically on every build: the three 'UAE detects Iranian missiles' reports -> 1 dot,
    # the two 'UAE halts trade with Iran' reports -> 1 dot, and the missile story must NOT fold into the trade
    # story (they share only {uae, iran}). This is the bug the user hit twice — dedup that relied on the AI net.
    _laE, _lnE = app.COUNTRY_COORDS["United Arab Emirates"]
    def _uae(t, cat, hrs, src):
        return {"title": t, "place": "UAE", "country": "United Arab Emirates", "lat": _laE, "lng": _lnE,
                "hrs": hrs, "cat": cat, "sources": [{"name": src, "url": "u" + src}], "sum": "", "image": ""}
    _uae_ev = [_uae("UAE says it detected 2 incoming ballistic missiles launched from Iran", "security", 1.5, "mem"),
               _uae("UAE defense ministry says Iran fired two ballistic missiles at territory", "security", 3.6, "ip"),
               _uae("UAE says its air defense systems have detected a missile threat", "security", 10.1, "ip2"),
               _uae("UAE halts all trade and financial transactions with Iran", "economy", 2.1, "disc"),
               _uae("UAE suspends all commercial activity and financial transactions with Iran", "politics", 3.3, "wg")]
    _uae_out = app._merge_same_event([dict(e) for e in _uae_ev])
    _reworded_ok = (len(_uae_out) == 2                                    # 3 missiles -> 1, 2 trade -> 1
                    and max(len(e.get("sources", [])) for e in _uae_out) == 3   # the missile dot cites all three
                    and len(app._merge_same_event([                       # two DIFFERENT centroid strikes stay apart
                        {"title": "Russia launches massive drone attack on Ukraine", "place": "Ukraine",
                         "country": "Ukraine", "lat": 49.0, "lng": 32.0, "hrs": 1.0, "cat": "security",
                         "sources": [{"name": "a", "url": "a"}], "sum": "", "image": ""},
                        {"title": "Russia fires missiles at Ukraine energy sites overnight", "place": "Ukraine",
                         "country": "Ukraine", "lat": 49.0, "lng": 32.0, "hrs": 2.0, "cat": "security",
                         "sources": [{"name": "b", "url": "b"}], "sum": "", "image": ""}])) == 2
                    # NAMESAKE GUARD (cold start, no AI): a WRONG-CONTINENT town (Lima, Peru) for a Ukraine war
                    # story drops to the named nationality's country; a correct nearby village is kept.
                    and app._locate("Russian forces shell Lima positions", "", "Ukrainian troops held the line", allow_ai=False)[3] == "Ukraine"
                    and app._locate("Russian Forces Capture Malaya Tokmachka", "", "Ukrainian forces withdrew", allow_ai=False)[3] == "Ukraine"
                    # NEW AREA = NEW DOT: two DIFFERENT southern-Lebanon towns fall on the same region centroid
                    # (the gazetteer can't pin either village) and share only strike boilerplate — they must
                    # stay TWO dots, not collapse a fresh strike into a stale one (`_is_area_place`+`_STRIKE_GENERIC`).
                    and app._is_area_place("Southern Lebanon, Lebanon") and not app._is_area_place("Kyiv, Ukraine")
                    and len(app._merge_same_event([
                        {"title": "Israeli Air Force airstrike against southern Lebanon town of Yohmor.",
                         "place": "Southern Lebanon, Lebanon", "country": "Lebanon", "lat": 33.3, "lng": 35.5,
                         "hrs": 0.1, "cat": "security", "sources": [{"name": "a", "url": "a"}], "sum": "", "image": ""},
                        {"title": "Israeli Air Force airstrike against southern Lebanon town of Aitaroun.",
                         "place": "Southern Lebanon, Lebanon", "country": "Lebanon", "lat": 33.3, "lng": 35.5,
                         "hrs": 0.3, "cat": "security", "sources": [{"name": "b", "url": "b"}], "sum": "", "image": ""}])) == 2
                    # ONE side pinned a village, the OTHER fell back to the region centroid ('Ali al-Taher'
                    # vs 'Bayout El Siyad', both southern Lebanon) — the area gate must look at BOTH places, or
                    # the two separate strikes collapse into one dot. The shared 'two/airstrikes' is boilerplate.
                    and len(app._merge_same_event([
                        {"title": "Two Israeli airstrikes targeted Bayout El Siyad, southern Lebanon",
                         "place": "Southern Lebanon, Lebanon", "country": "Lebanon", "lat": 33.4, "lng": 35.5,
                         "hrs": 0.2, "cat": "security", "sources": [{"name": "a", "url": "a"}], "sum": "", "image": ""},
                        {"title": "Two Israeli Air Force airstrikes against Ali al-Taher Hill, southern Lebanon",
                         "place": "Ali Al Taher, Lebanon", "country": "Lebanon", "lat": 33.38, "lng": 35.48,
                         "hrs": 0.1, "cat": "security", "sources": [{"name": "b", "url": "b"}], "sum": "", "image": ""}])) == 2
                    # A PERSON'S DEATH is ONE story wherever datelined: a shared 2-token NAME + near-identical
                    # wording merges even across countries (a US death a UK wire mis-datelined). SHIPPED BUG: two
                    # Dolly-Parton death dots (one mis-dotted UK, one US) stood apart.
                    and len(app._merge_same_event([
                        {"title": "US country music legend Dolly Parton dies aged 80", "place": "United Kingdom",
                         "country": "United Kingdom", "lat": 54.0, "lng": -2.0, "hrs": 2.0, "cat": "society",
                         "sources": [{"name": "a", "url": "a"}], "sum": "", "image": ""},
                        {"title": "Dolly Parton has died aged 80.", "place": "United States",
                         "country": "United States of America", "lat": 39.8, "lng": -98.6, "hrs": 1.5,
                         "cat": "society", "sources": [{"name": "b", "url": "b"}], "sum": "", "image": ""}])) == 1
                    # GUARD: two DIFFERENT templated events in different countries (no shared distinctive name)
                    # must NEVER merge cross-country.
                    and len(app._merge_same_event([
                        {"title": "Earthquake kills dozens in Turkey", "place": "Turkey", "country": "Turkey",
                         "lat": 39, "lng": 35, "hrs": 2.0, "cat": "climate", "sources": [{"name": "a", "url": "a"}], "sum": "", "image": ""},
                        {"title": "Earthquake kills dozens in Japan", "place": "Japan", "country": "Japan",
                         "lat": 36, "lng": 138, "hrs": 1.5, "cat": "climate", "sources": [{"name": "b", "url": "b"}], "sum": "", "image": ""}])) == 2)
    _me_ok = (len(_m) == 2                                                # 3 Kyiv reports -> 1, + Odesa
              and _kdot is not None and len(_kdot.get("sources", [])) == 3
              and _kdot["sources"][0]["name"] == "France 24"             # first reporter is the primary source
              and _kdot["title"].startswith("Kyiv: 9 dead")             # ...and its headline leads
              and _kdot["hrs"] == 3.4                                    # dot stays fresh (latest update)
              and _diff_ok                                               # Komsomolsk-vs-Orsk stay two dots
              and _reworded_ok                                           # UAE missile x3 -> 1, trade x2 -> 1, kept apart
              and app._death_toll("barrage kills nine in Kyiv") == 9
              and app._death_toll("markets rose 3 percent today") is None)
    ran[0] += 1
    if not _me_ok:
        fails.append(("merge", "kyiv-3-sources", "1 dot, 3 sources, France 24 first",
                      f"dots={len(_m)} kdot_sources={len(_kdot.get('sources', [])) if _kdot else 0}",
                      "three outlets covering '9 dead in Kyiv' must merge into one cited dot; Odesa stays separate"))
    print(f"  {'ok ' if _me_ok else 'FAIL'} {len(_m)} dots; Kyiv sources="
          f"{[s['name'] for s in (_kdot.get('sources', []) if _kdot else [])]}")

    # SELF-LEARNING GAZETTEER — the AI names the exact town the rules can't pin; we geocode it once and
    # remember the coords forever, so it becomes a free, deterministic, COLD-START hit next time. Fully
    # offline here: the geocoder and the AI-WHERE are stubbed, and _learn_place is redirected to the
    # in-memory dict so the test writes NO files.
    print("\n=== SELF-LEARNING GAZETTEER (learned places + confidence) ===")
    _lg_fails = []
    # (1) confidence: only a broad REGION/WATER centroid is 'low' (approximate). A specific city is 'high',
    # and a bare COUNTRY centroid is 'high' too — for a NATIONAL story the country is the right level, so
    # flagging ~50 of them was noise (the user's complaint). Only a region we couldn't pin within is 'approx'.
    _lg_fails.append(("conf-high", app._geo_confidence((33.38, 35.48, "Deir Seryan, Lebanon", "Lebanon")) == "high"))
    _lg_fails.append(("conf-region", app._geo_confidence((33.4, 35.5, "Southern Lebanon, Lebanon", "Lebanon")) == "low"))
    _laL, _lnL = app.COUNTRY_COORDS.get("Lebanon", (33.8, 35.8))
    _lg_fails.append(("conf-country-not-flagged", app._geo_confidence((_laL, _lnL, "Lebanon", "Lebanon")) == "high"))
    # LEADER STATEMENT -> CAPITAL (offline, rules only). A quote/threat with no scene dots the speaker's
    # capital; a statement that NAMES a scene keeps it; a non-statement is untouched.
    _lg_fails.append(("capital-putin", "Moscow" in (app._locate(
        "Putin threatened further attacks on Ukraine, saying attempts to disrupt Russian logistics would be met with strikes", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("capital-keeps-scene", "Moscow" not in (app._locate(
        "Putin says Russian forces captured Avdiivka in Donetsk region", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("capital-not-for-strike", "Moscow" not in (app._locate(
        "Russian forces shell Kharkiv overnight", "", "", allow_ai=False)[2] or "")))
    # MARITIME STRIKE -> the WATER (the ship's location), overriding the actor's country/capital — so it also
    # co-locates with, and merges into, the other tanker-strike dot instead of standing apart on Sana'a.
    _lg_fails.append(("maritime-water", "Red Sea" in (app._locate(
        "Yemen's Houthis claim strikes on Saudi oil tanker, troop concentrations", "",
        "targeting a Saudi oil tanker in the Red Sea and troop concentrations in eastern Yemen", allow_ai=False)[2] or "")))
    _lg_fails.append(("maritime-guard-land", "Samara" in (app._locate(
        "Ukrainian drone strikes an oil refinery in Samara", "", "", allow_ai=False)[2] or "")))
    # US FINANCIAL MARKETS -> NEW YORK (Wall Street), overriding a country cited only as a market driver.
    _lg_fails.append(("markets-nyc", "New York" in (app._locate(
        "Stocks stagger and oil rises as traders eye Iran threat, Nvidia results", "",
        "Stocks fell on a US plan for the economic asphyxiation of Iran, while tech firms struggled after a "
        "down day on Wall Street ahead of earnings from Nvidia.", allow_ai=False)[2] or "")))
    _lg_fails.append(("markets-nyc-wallst", "New York" in (app._locate(
        "Wall Street rallies as the Dow hits a record high", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("markets-guard-foreign", "New York" not in (app._locate(
        "Tokyo stocks close higher as the Nikkei gains", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("markets-guard-strike", "New York" not in (app._locate(
        "US could carry out further strikes on Iran", "", "", allow_ai=False)[2] or "")))
    # A PERSON'S OBITUARY dots the person's OWN country (the nationality in the title), never a country that
    # only paid tribute. SHIPPED BUG: "US … Dolly Parton dies aged 80" dotted the UK (a body tribute).
    _lg_fails.append(("obit-nationality", (app._locate(
        "US country music legend Dolly Parton dies aged 80", "",
        "The family announced her passing. Tributes came from British fans across the United Kingdom.",
        allow_ai=False)[3]) == "United States of America"))
    _lg_fails.append(("obit-is", app._is_obituary("US country music legend Dolly Parton dies aged 80")))
    _lg_fails.append(("obit-guard-casualties", not app._is_obituary("50 dead in Kabul suicide bombing")))
    # A NAMED facility wins over the capital: the Amur Gas Chemical Complex (far-east Amur Oblast), not Moscow.
    _lg_fails.append(("amur-complex", "Amur" in (app._locate(
        "More than 100 people were injured in an explosion at the Amur Gas Chemical Complex construction site, Russian media report",
        "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("amur-not-moscow", "Moscow" not in (app._locate(
        "Explosion at the Amur Gas Chemical Complex construction site injures dozens", "", "", allow_ai=False)[2] or "")))
    # GEORGIA the US STATE vs the country: a Savannah/GBI story is the US state, not the Caucasus.
    _lg_fails.append(("georgia-us-state", (app._locate(
        "4 Police Department Employees Arrested on Charges of Misusing Flock Camera System in Georgia",
        "", "Four former Savannah Police Department employees were arrested following a Georgia Bureau of Investigation (GBI) probe.",
        allow_ai=False)[3]) == "United States of America"))
    _lg_fails.append(("georgia-country-guard", (app._locate(
        "Mass protests erupt in Georgia over disputed election", "",
        "Demonstrators gathered in Tbilisi as the Georgian Dream party claimed victory.", allow_ai=False)[3]) == "Georgia"))
    # CONTAINMENT via the DESC: the title names only the COUNTRY, but the body hands us the specific scene
    # ("in Bihar's capital"). SHIPPED BUG: dotted New Delhi. The "'s capital" possessive had SUNK Bihar.
    _lg_fails.append(("desc-containment", "Bihar" in (app._locate(
        "India police clash with protesters a month after Gen Z demonstrations", "",
        "Police used water cannons as protesters broke through barricades in Bihar's capital.", allow_ai=False)[2] or "")))
    _lg_fails.append(("desc-containment-noabroad", (app._locate(     # a body place ABROAD must NOT hijack
        "India summons envoy over remarks", "", "The ministry acted after comments made in Paris.",
        allow_ai=False)[3]) == "India"))
    # A VESSEL strike happens AT SEA — a Houthi/Yemen tanker strike lands on the Red Sea, never the target's
    # capital, even when the same line "warns Riyadh"; the physical strike beats the statement upgrade.
    _lg_fails.append(("vessel-strike-water", app._is_water_place(app._locate(
        "Yemen strikes Saudi oil tanker, warns Riyadh of graveyard and hell", "",
        "Yemeni commanders warned Saudi Arabia.", allow_ai=False)[2] or "")))
    _lg_fails.append(("vessel-strike-not-riyadh", "Riyadh" not in (app._locate(
        "Yemen strikes Saudi oil tanker, warns Riyadh of graveyard and hell", "", "", allow_ai=False)[2] or "")))
    # GUARD: a pure THREAT ("warns of strikes") still resolves to the speaker's capital.
    _lg_fails.append(("threat-still-capital", "Moscow" in (app._locate(
        "Putin warns of strikes on Ukraine if talks fail", "", "", allow_ai=False)[2] or "")))
    # ASYLUM / RESETTLEMENT dots where the person ENDED UP (the country that granted asylum), not the one fled.
    _lg_fails.append(("asylum-destination", (app._locate(
        "Christian Convert Who Fled Iran and Was Deported By Trump Finds New Home", "",
        "She was deported to Panama under Trump's crackdown. Now, Canada has given her asylum.",
        allow_ai=False)[3]) == "Canada"))
    _lg_fails.append(("fled-origin-sunk", (app._locate(     # a country FLED is the origin, not the scene
        "Refugees flee Sudan war", "", "Tens of thousands crossed into Chad.", allow_ai=False)[3]) == "Chad"))
    _lg_fails.append(("asylum-guard-policy", (app._locate(   # a US asylum-POLICY story stays in the US
        "Trump tightens US asylum rules at the border", "", "New restrictions take effect Monday.",
        allow_ai=False)[3]) == "United States of America"))
    # The SCENE named in the headline/body wins over the actor/source. All literally named.
    _lg_fails.append(("passive-agent-scene", "Petersburg" in (app._locate(   # "targeted BY Ukraine near St. Petersburg"
        "ICRC delegates visit facilities targeted by Ukraine near St. Petersburg", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("passive-agent-guard", (app._locate(   # GUARD: "hit BY Russia IN Ukraine" -> the scene Ukraine
        "Power facilities hit by Russia in Ukraine", "", "", allow_ai=False)[3]) == "Ukraine"))
    _lg_fails.append(("tibet-region", (app._locate(          # a bare "in Tibet" dots China, not the source (Russia)
        "Three killed, 265 missing after mudslide in Tibet", "Russia", "", allow_ai=False)[3]) == "China"))
    _lg_fails.append(("vaca-muerta-argentina", (app._locate(  # the Argentine gas field, not a US "Rincon"
        "Two Workers Killed in YPF Vaca Muerta Accident at Rincon del Mangrullo", "",
        "at YPF's Rincon del Mangrullo gas plant in Vaca Muerta, Argentina's shale basin.", allow_ai=False)[3]) == "Argentina"))
    _lg_fails.append(("envoy-posting", (app._locate(          # "X's envoy TO Iran" -> the posting (Iran), not X
        "Japan's Envoy to Iran on Conflict and Diplomacy", "", "", allow_ai=False)[3]) == "Iran"))
    # An ambiguous COMPANY name maps to its disambiguated Wikipedia title (the firm, not the fruit/river).
    _lg_fails.append(("company-wiki", app._ORG_WIKI.get("apple") == "Apple Campus" and app._ORG_WIKI.get("amazon") == "Amazon.com"))
    # COMPANY-INTERNAL news (a hire / exec move / earnings) dots the company's HEADQUARTERS, not the country
    # centroid or capital. SHIPPED BUG: "Barret Zoph joins Google" dotted Washington D.C.; belongs at Google HQ.
    _lg_fails.append(("hq-google-hire", "Mountain View" in (app._locate(
        "Barret Zoph joins Google", "", "The Thinking Machines Lab co-founder is moving to Google.",
        allow_ai=False)[2] or "")))
    _lg_fails.append(("hq-apple-exec", "Cupertino" in (app._locate(
        "Apple names new head of hardware engineering", "", "", allow_ai=False)[2] or "")))
    _lg_fails.append(("hq-samsung-korea", (app._locate(
        "Samsung appoints new co-CEO", "", "", allow_ai=False)[3]) == "South Korea"))
    # GUARD: a company story that NAMES a country stays there — "Google launches service in Nigeria" is Nigeria,
    # not Mountain View (the HQ rule fills only a story whose sole geographic signal IS the company).
    _lg_fails.append(("hq-guard-named-country", (app._locate(
        "Google launches new payment service in Nigeria", "", "", allow_ai=False)[3]) == "Nigeria"))
    # GUARD: a fine/lawsuit is NOT company-internal — an EU antitrust penalty must never land on the HQ.
    _lg_fails.append(("hq-guard-not-internal", "Mountain View" not in (app._locate(
        "Google fined 2 billion euros by EU antitrust regulators", "",
        "The European Commission announced the penalty.", allow_ai=False)[2] or "")))
    # GUARD: a specific scene the story names (a store opening in Mumbai) wins over the HQ.
    _lg_fails.append(("hq-guard-scene", "Mumbai" in (app._locate(
        "Apple opens new store in Mumbai", "", "", allow_ai=False)[2] or "")))
    # A named Mexican state resolves (a coastal-tourism story dotted the capital before); the body still refines.
    _lg_fails.append(("yucatan-state", (app._geolocate("Yucatan", "", "") or ["", "", "", ""])[3] == "Mexico"))
    _lg_fails.append(("yucatan-body-scene", "Chuburna" in (app._locate(   # the body's town wins over the country
        "Construction on tourist project on Yucatan coast halted", "",
        "at the site near Chuburna Puerto, 15 km from the beach town of Progreso.", allow_ai=False)[2] or "")))
    _orig_aw, _orig_geo, _orig_learn = app._ai_where, app._geocode_nominatim, app._learn_place
    _injected = []
    try:
        # write to the in-memory store only — never touch disk during tests
        app._learn_place = lambda name, lat, lng, place, country: (
            app._LEARNED_PLACES.__setitem__(app._lp_key(name),
                {"lat": lat, "lng": lng, "place": place, "country": country}),
            _injected.append(app._lp_key(name)))
        # (2) COLD START (allow_ai=False, no network): a place already LEARNED resolves to its exact coords
        app._LEARNED_PLACES[app._lp_key("Deir Seryan, Lebanon")] = {
            "lat": 33.281, "lng": 35.44, "place": "Deir Seryan, Lebanon", "country": "Lebanon"}
        _injected.append(app._lp_key("Deir Seryan, Lebanon"))
        app._ai_where = lambda t: "Deir Seryan, Lebanon" if "Deir Seryan" in t else _orig_aw(t)
        _cold = app._locate("Israeli airstrike on Deir Seryan, southern Lebanon", "",
                            "Israeli airstrike on Deir Seryan, southern Lebanon", allow_ai=False)
        _lg_fails.append(("learned-cold-start", bool(_cold) and round(_cold[0], 2) == 33.28
                          and app._geo_confidence(_cold) == "high"))
        # (3) LIVE: a NEW town is geocoded once (stubbed), anchored to the story's country, and LEARNED
        app._geocode_nominatim = lambda q: (33.30, 35.47, "Lebanon") if "Bayout" in q else None
        app._ai_where = lambda t: "Bayout El Siyad, Lebanon" if "Bayout" in t else _orig_aw(t)
        _live = app._locate("Two Israeli airstrikes targeted Bayout El Siyad, southern Lebanon", "",
                            "Two Israeli airstrikes targeted Bayout El Siyad, southern Lebanon", allow_ai=True)
        _lg_fails.append(("nominatim-learn", bool(_live) and round(_live[0], 2) == 33.30
                          and app._geo_confidence(_live) == "high"))
        _lg_fails.append(("persisted", app._learned_place_lookup("Bayout El Siyad, Lebanon") is not None))
        # (4) GUARD: a geocode to the WRONG country (hallucinated town) is rejected — no false pin
        app._geocode_nominatim = lambda q: (48.85, 2.35, "France")   # Paris coords for a Lebanon story
        app._ai_where = lambda t: "Nowheresville, Lebanon" if "Nowheresville" in t else _orig_aw(t)
        _bad = app._locate("Strike on Nowheresville, southern Lebanon", "",
                           "Strike on Nowheresville, southern Lebanon", allow_ai=True)
        _lg_fails.append(("reject-wrong-country", not (_bad and abs(_bad[0] - 48.85) < 0.1)))
    finally:
        app._ai_where, app._geocode_nominatim, app._learn_place = _orig_aw, _orig_geo, _orig_learn
        for k in _injected:
            app._LEARNED_PLACES.pop(k, None)
    for _name, _ok in _lg_fails:
        ran[0] += 1
        if not _ok:
            fails.append(("learn-geo", _name, "pass", "FAIL", "self-learning gazetteer / confidence"))
        print(f"  {'ok ' if _ok else 'FAIL'} {_name}")

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

    # PROMOTION PAIRS HEADLINE + BODY. SHIPPED BUG: title/url were promoted but 'sum' only "if dup.get('sum')",
    # so an earlier report with NO wire description left the EARLIER headline over the LATER, different story's
    # body — "DPRK slams US-ROK drills" sitting over a US gasoline-price paragraph. The promoted headline and
    # teaser must always come from the SAME story (empty is fine — the baked brief refills it).
    _gas = {"source": "MEM", "domain": "mem.com", "url": "https://mem.com/gas", "country": "United States",
            "involved": ["United States", "Iran"], "hrs": 2.0,
            "title": "US gasoline prices climb above $4 a gallon",
            "sum": "The average gasoline price in the US climbed above $4 per gallon."}
    _dprk = {"source": "CGTN", "domain": "cgtn.com", "url": "https://cgtn.com/dprk", "hrs": 3.0, "sum": "",
             "title": "DPRK slams upcoming US-ROK drills as rehearsal for aggressive war"}
    app._cite_source(_gas, _dprk)
    _pair_ok = ("DPRK" in _gas["title"]                          # earlier reporter's headline promoted
                and "gasoline" not in (_gas["sum"] or "").lower()   # ...and the OTHER story's body did NOT stay
                and _gas["url"] == "https://cgtn.com/dprk")       # link matches the shown headline
    ran[0] += 1
    if not _pair_ok:
        fails.append(("merge", "promotion-pairs-title-body", "no Frankenstein card",
                      f"title={_gas['title'][:30]!r} sum={_gas['sum'][:30]!r}",
                      "a promoted headline must never sit above a different story's body"))
    print(f"  {'ok ' if _pair_ok else 'FAIL'} promotion pairs title+body -> sum={_gas['sum']!r}")

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
        app._ai_same_event = lambda a, b, cache_only=False: ("black sea" in (a["title"] + b["title"]).lower()
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
    # COLD-START cache-only pass with NO learned verdicts merges nothing and never calls the LLM (the real
    # _ai_same_event reads the empty verdict cache -> None -> no merge), so a fresh feed is returned untouched.
    _cacheonly_noop = len(app._ai_dedup([dict(e) for e in _sd], cache_only=True)) == 3
    # a long ROUNDUP/stream video is not single-event footage; a short clip is kept
    _dur_ok = (app._dur_minutes("24:46") > 12 and app._dur_minutes("1:02:00") > 12
               and app._dur_minutes("0:22") <= 12 and app._dur_minutes("3:10") <= 12 and app._dur_minutes("Video") == 0)
    _sem_ok = _sem_ok and _noop and _cacheonly_noop and _dur_ok
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

    # TEXT SHARPEN — a wire post's inline image/agency credits are stripped and a mid-air headline gets a
    # terminal stop, while legit brackets and speaker labels are left alone.
    print("\n=== TEXT SHARPEN (credits stripped, end-stop added) ===")
    _c1 = app._tg_clean("Gaza beekeepers rebuild on rooftops [Screengrab/AA] [Photo/AA] after attacks")
    _c2 = app._tg_clean("Israeli forces arrest a Palestinian as settlers attack civilians in Nablus and Bethlehem")
    _c3 = app._tg_clean("He called the deal \"done\" [sic] on Tuesday")
    _c4 = app._tg_clean("Lavrov:")
    # RSS descriptions bypass _tg_clean — _sharpen_desc gives them the SAME treatment (the shipped bug)
    _r1 = app._sharpen_desc("Chinese AI startup Moonshot's flagship model bypassed a UK safety test, researchers say")
    _r2 = app._sharpen_desc("Migrants begin returning to Morocco on July 31, 2026. [Abu Adem Muhammed – Anadolu Agency]")
    _f1 = app._tg_clean("\U0001F1FE\U0001F1EA - Additional scenes from the clashes in southwest Yemen show heavy fire")
    _f2 = app._strip_lead_flag("\U0001F91D SA — Video: the Crown Prince meets the President")   # emoji + country code tag
    _f3 = app._strip_lead_flag("US - based firm expands abroad")                                     # NOT a tag (no emoji) -> untouched
    # A reposter's "Name via Platform:" attribution stamp is dropped; ordinary prose with 'via'/'on' is kept.
    _v1 = app._strip_lead_flag("President Trump via Truth Social: I see that Iran is asking for reparations.")
    _v2 = app._sharpen_desc("Netanyahu via Telegram: Our forces are ready for any scenario.")
    _v3 = app._strip_lead_flag("A report on climate change: the data is clear.")   # 'on <topic>:' is not a platform -> kept
    # A speaker's "Name: quote" opener becomes reported speech; a topic label ('Gaza:', 'BREAKING:') is left alone.
    _s1 = app._fix_speaker_colon("Former Israeli PM Naftali Bennett: Qatar is defeating Israel.")
    _s2 = app._fix_speaker_colon("Trump to Axios: We are low-keying it with Iran.")   # outlet dropped, opener lowered
    _s3 = app._fix_speaker_colon("Gaza: 20 killed in an overnight strike.")           # topic label, not a speaker
    # A source's trailing "[...]" / "(…)" truncation stamp is dropped with the fragment it clipped; text with
    # no stamp keeps the gentle end-stop instead of being trimmed.
    _t1 = app._sharpen_desc("The senator died last month. Hegseth called for a bigger budget [...]")
    _t2 = app._sharpen_desc("Researchers found the model bypassed a safety test, they say")   # no stamp
    _sharp_ok = ("[" not in _c1 and "AA]" not in _c1                     # credit tags gone
                 and _c2.endswith(".")                                   # mid-air headline gets a full stop
                 and "[sic]" in _c3                                      # a real editorial bracket survives
                 and not _c4.endswith(".")                              # a bare speaker label is left alone
                 and _r1.endswith("say.")                               # RSS desc gets an end stop too
                 and "[" not in _r2 and _r2.endswith("2026.")           # RSS credit bracket stripped
                 and _f1.startswith("Additional")                       # leading country-flag emoji tag stripped
                 and _f2.startswith("Video:")                           # emoji + "SA —" country-code tag stripped
                 and _f3.startswith("US - based")                       # a real "US - " (no emoji) is left alone
                 and _v1.startswith("I see that")                        # "President Trump via Truth Social:" stamp dropped
                 and _v2.startswith("Our forces")                       # "Netanyahu via Telegram:" stamp dropped in an RSS desc too
                 and _v3.startswith("A report on climate")              # ordinary 'on <topic>:' prose is untouched
                 and _s1 == "Former Israeli PM Naftali Bennett says Qatar is defeating Israel."  # colon -> reported speech
                 and _s2 == "Trump says we are low-keying it with Iran."  # "to Axios" outlet dropped, "We"->"we"
                 and _s3 == "Gaza: 20 killed in an overnight strike."    # a topic label is NOT a speaker -> untouched
                 and "[" not in _t1 and _t1.endswith("last month.")     # "[...]" stamp + dangling fragment dropped
                 and _t2.endswith("say.")                               # no stamp -> gentle end-stop, not trimmed
                 and app._sharpen_desc("against Iran earlier this year. TJP reports that Iran kept its missile capabilities.").startswith("TJP reports")   # lower-case sentence TAIL dropped -> starts at the whole sentence
                 and app._sharpen_desc("Students went on a field trip to the museum...") == "Students went on a field trip to the museum."   # a teaser "..." is dropped and the sentence is finished, never shipped mid-thought
                 and not app._sharpen_desc("The council met to discuss the budget and then...").endswith("...")  # trailing ellipsis never survives to the card
                 # a RAW URL / "JUST IN -" / "Source:" pointer never survives to the card (the sloppy Disclose teaser)
                 and app._sharpen_desc("JUST IN - UAE halts all trade and financial transactions with Iran. Source: https://x.com/mofauae/status/2089808983880327494?s=46") == "UAE halts all trade and financial transactions with Iran."
                 and "http" not in app._sharpen_desc("Iran fired missiles at the UAE overnight. More at https://t.me/insiderpaper")
                 # a bare trailing OUTLET byline and an inline self-promo ("DW has more") are furniture, not news
                 and app._sharpen_desc("Fire in a building housing several hotels in eastern India kills 9 and injures 6 AP News.") == "Fire in a building housing several hotels in eastern India kills 9 and injures 6."
                 and app._sharpen_desc("At least nine people have died, including some Bangladeshi nationals. DW has more.") == "At least nine people have died, including some Bangladeshi nationals."
                 and "South China Morning Post" in app._sharpen_desc("The summit was hosted by South China Morning Post founder Robert Kuok in Hong Kong.")  # a real proper noun mid-sentence is kept
                 # a sentence that already ends inside a closing quote must NOT get a second full stop (the '…operating.".' bug)
                 and app._sharpen_desc('JUST IN - Trump says the blockade remains, "The Hormuz Strait is open and operating." Source: https://truthsocial.') == 'Trump says the blockade remains, "The Hormuz Strait is open and operating."'
                 and not app._end_stop('The strait is open and operating."').endswith('".')
                 # a SHORT teaser truncated mid-sentence is cut back to the last WHOLE sentence, never shipped as a
                 # stub with a tacked-on period ("…oil is down today a.") — the shipped bug across many cards.
                 and app._sharpen_desc("The US said keeping oil prices low is its top priority, ahead of Iran's nuclear program. “I know that oil is down today a...").endswith("nuclear program.")
                 and app._sharpen_desc("A gunman opened fire at a market. At least nine people died. Police say the suspect fle") == "A gunman opened fire at a market. At least nine people died."
                 and app._sharpen_desc("imagery also shows significant damage to the base.")[:1].isupper()  # a lone lower-case fragment is capitalized, not shipped mid-thought
                 # FULL BLOCK on leading junk: flag emojis (regional indicators that render as "IROM"), a
                 # lightning bolt, a double-dash and a promo word hiding BEHIND them all get peeled off.
                 and app._sharpen_desc("\U0001F1EE\U0001F1F7\U0001F1F4\U0001F1F2 ⚡ — NEW: UKMTO reports a vessel was struck near Hormuz.") == "UKMTO reports a vessel was struck near Hormuz."
                 and app._sharpen_desc("\U0001F1FA\U0001F1E6 UPDATE — Ukrainian forces repelled an assault near Pokrovsk.") == "Ukrainian forces repelled an assault near Pokrovsk."
                 # INLINE emoji separators a channel uses mid-text ("People ➡️ … ➡️ … 📝 …") are stripped too, not
                 # just leading ones. SHIPPED BUG: the Nepal flash-flood brief rendered "People ➡️ Horrific footage…".
                 and "➡" not in app._sharpen_desc("People ➡️ Horrific footage shows a flood. ➡️ Villages were submerged. \U0001f4dd At least 150 dead.")
                 and "\U0001f4dd" not in app._sharpen_desc("Rescue teams search the valley. \U0001f4dd Hundreds remain missing.")
                 # Google-News aggregator boilerplate is not a story -> treated as EMPTY, never shown as a brief.
                 and app._sharpen_desc("Comprehensive up-to-date news coverage, aggregated from sources all over the world by Google News.") == "")
    # WHO IS REPORTING — factual, even-handed ownership notes; ordinary/independent outlets get none.
    _srcnote_ok = (app._source_note("TASS", "tass.com") == "Russian state media"
                   and app._source_note("CGTN", "cgtn.com") == "Chinese state media"
                   and app._source_note("Al Jazeera", "aljazeera.com") == "Qatari state-funded"
                   and app._source_note("BBC", "bbc.co.uk") == "UK public broadcaster"
                   and app._source_note("Reuters", "reuters.com") == ""            # independent -> no label
                   and app._indepth_source("The New York Times", "nytimes.com")    # in-depth outlet -> longer brief
                   and app._indepth_source("Premium Times", "premiumtimesng.com")  # a national paper -> fuller brief too
                   and not app._indepth_source("Rerum Novarum", "rerumnovarum.substack.com"))
    # SPAM / SCAM / channel-plug posts (a crypto-signals ad, a WhatsApp-invite pump) are dropped; real news kept.
    _spam_ok = (app._is_spam("Join this Bitcoin platform for BTC market signals before everyone else catches on. JOIN AND READ HERE: https://chat.whatsapp.com/FK21")
                and app._is_spam("DM us to join our VIP signals group for guaranteed profit")
                and not app._is_spam("Bitcoin hits $90k as US SEC approves a spot ETF, Reuters reports")
                and not app._is_spam("Russia strikes Kyiv, 12 killed"))
    _srcnote_ok = _srcnote_ok and _spam_ok
    # BYLINE STRIP — a short "… - Reuters" headline loses its outlet suffix too (was only stripped over 55 chars),
    # while a compound ("anti-corruption") and a tiny head ("War - what is it") are left intact.
    _srcnote_ok = _srcnote_ok and (app._clean_headline("UAE says Iran launched two missiles at it - Reuters") == "UAE says Iran launched two missiles at it"
                                   and app._clean_headline("Suspect detained in anti-corruption sweep") == "Suspect detained in anti-corruption sweep")
    _sharp_ok = _sharp_ok and _srcnote_ok
    # A wire teaser truncated mid-sentence on a connector ("…in Kiryat Gat, following") must not become
    # "…following." — drop the dangling connector. And wire abbreviations spell out ("bln"->"billion").
    _memo = app._sharpen_desc("Israeli FM Saar ordered the expulsion of Dutch representatives from the centre in Kiryat Gat, following")
    _sharp_ok = _sharp_ok and _memo.endswith("Kiryat Gat.") and "following" not in _memo
    _sharp_ok = _sharp_ok and app._sharpen_desc("The payout could total 107.79 bln rubles.") == "The payout could total 107.79 billion rubles."
    _sharp_ok = _sharp_ok and app._sharpen_desc("Rescuers pulled survivors from the rubble after the blast.").endswith("after the blast.")  # GUARD: a complete sentence is untouched
    # A FULL first paragraph (what <content:encoded> gives) survives WHOLE — no mid-sentence cut. This is the
    # permanent MEMO fix: the parser prefers content:encoded over the truncated <description> excerpt.
    _memo_full = app._sharpen_desc("The UN Committee on the Elimination of Racial Discrimination (CERD) expressed alarm over statements by Israeli officials threatening to impose on Lebanon the same level of destruction as inflicted in Gaza and called for an end to rhetoric.")
    _sharp_ok = _sharp_ok and "same level of destruction" in _memo_full and _memo_full.rstrip().endswith("rhetoric.")
    # A first-person op-ed LEDE in the body is fluff even when the headline looks like news.
    _sharp_ok = _sharp_ok and app._is_fluff("Syrian Kurds and the region", "", "When I look back at my university years, I am reminded of a country that understood unity.")
    _sharp_ok = _sharp_ok and not app._is_fluff("Israel expels Dutch officials from Gaza", "", "Israeli FM Saar ordered the immediate expulsion of Dutch representatives.")  # GUARD
    ran[0] += 1
    if not _sharp_ok:
        fails.append(("sharpen", "credits+endstop", "brackets gone, period added, [sic]/label kept",
                      f"c1={_c1!r} c2_end={_c2[-1:]!r} c3_has_sic={'[sic]' in _c3} c4={_c4!r}",
                      "image/agency credits must be stripped and a fragment given an end stop"))
    print(f"  {'ok ' if _sharp_ok else 'FAIL'} {_c1!r}")

    # LIVE TV — the current-live video id is pulled from a channel's /live <link rel=canonical> (offline: the
    # regex + channel-list integrity; the live scrape itself is network and not unit-tested).
    print("\n=== LIVE TV (video-id extraction + channel list) ===")
    _tv_live = '<link rel="canonical" href="https://www.youtube.com/watch?v=gCNeDWCI0vo">'   # channel IS live -> a watch id
    _tv_off  = '<link rel="canonical" href="https://www.youtube.com/@dwnews">'                # offline -> channel page, no id
    _m = app._LIVE_TV_VID_RE.search(_tv_live)
    _livetv_ok = (bool(_m) and _m.group(1) == "gCNeDWCI0vo"                       # live canonical -> the 11-char id
                  and not app._LIVE_TV_VID_RE.search(_tv_off)                     # channel-page canonical -> no id (greyed as off-air)
                  and len(app._LIVE_TV_CHANNELS) >= 8                             # a real roster
                  and all(c.get("name") and c.get("handle") and c.get("cat") and c.get("cc") for c in app._LIVE_TV_CHANNELS)  # each has a flag code
                  and all("note" in c for c in app._LIVE_TV_CHANNELS)             # every channel carries an (even-handed, maybe empty) ownership caption
                  and len({c["handle"] for c in app._LIVE_TV_CHANNELS}) == len(app._LIVE_TV_CHANNELS))  # no dup handle
    ran[0] += 1
    if not _livetv_ok:
        fails.append(("live-tv", "id-extract", "extract id from live canonical; none from a channel page; clean roster",
                      f"m={_m.group(1) if _m else None} chans={len(app._LIVE_TV_CHANNELS)}",
                      "Live TV resolves each channel's current live video id, self-healing a rotated stream"))
    print(f"  {'ok ' if _livetv_ok else 'FAIL'} id-from-live-canonical + {len(app._LIVE_TV_CHANNELS)} channels, no dup handles")

    # WHO'S INVOLVED — the glossary detects the groups a story names, in order, without false-firing on
    # ordinary words ('AP', 'map'), and returns a fair (non-labelling) definition.
    print("\n=== WHO'S INVOLVED (glossary term detection) ===")
    _g1 = app._glossary_terms("Houthis attack a ship in the Red Sea as the IRGC vows to respond")
    _g2 = app._glossary_terms("AP publishes a new map of the region")
    _terms1 = [t["term"] for t in _g1]
    # AI long tail: capitalized 'Proper Name + org word' phrases are DETECTED (then AI-defined); lowercase
    # 'government forces' and a bare word are not.
    _det = app._detect_org_phrases("The Southern Popular Resistance Army held while Yemeni government forces regrouped.", set())
    _gloss_ok = (len(_g1) == 2 and "Houthi" in _terms1[0]
                 and "Revolutionary Guard" in _terms1[1]
                 and "terrorist" not in _g1[0]["def"].lower()          # fair definition, never a label
                 and _g2 == []                                          # no false-fire on 'AP'/'map'
                 and "Southern Popular Resistance Army" in _det         # org phrase detected for the AI tail
                 and "Cockroach Janta Party" in app._detect_org_phrases("The Cockroach Janta Party won a victory when the minister resigned.", set())  # a PARTY is an org too -> defined
                 # the WHOLE name, incl. the "of Britain" tail — not a truncated "Muslim Association" (shipped bug)
                 and "Muslim Association of Britain" in app._detect_org_phrases("The Muslim Association of Britain urge the UK government to act", set())
                 and "DFAT" in app._detect_org_phrases("Australian dies in Vietnam, DFAT confirms consular assistance", set())   # a bare acronym -> defined
                 and app._detect_org_phrases("The US and UK and NATO met the UN and EU on GDP", set()) == []   # common acronyms are NOT flagged
                 and "TIMES" not in app._detect_org_phrases("PREMIUM TIMES reported the commission uncovered a fake agency", set())   # SHIPPED: an OUTLET name is not an org to define
                 and "POST" not in app._detect_org_phrases("THE POST said officials confirmed the arrest", set())
                 and "IRGC" in app._detect_org_phrases("The IRGC held a parade in Tehran", set())   # GUARD: a real acronym is still defined
                 and "MOEX" in app._detect_org_phrases("The MOEX Index lost to 2071 points, the RTS Index fell", set())   # a 3+-cap acronym is detected...
                 and any("Moscow Exchange" in t["term"] for t in app._glossary_terms("The MOEX Index fell today"))   # ...and MOEX has a curated definition
                 and any("dollars" in t["def"] for t in app._glossary_terms("The RTS Index fell today"))   # RTS too (dollar-priced)
                 # SHIPPED BUG: a bare capitalised "Armed Forces" (from "French Ministry of Armed Forces") was
                 # bolded as some org/leader. Generic force/guard bodies are plain English -> never defined...
                 and "Armed Forces" not in app._detect_org_phrases("The French Ministry of Armed Forces confirmed the deployment continues", set())
                 and "Security Forces" not in app._detect_org_phrases("Security Forces detained several people overnight", set())
                 # ...but a SPECIFIC named body (3+ words) is still defined
                 and "Israel Defense Forces" in app._detect_org_phrases("The Israel Defense Forces struck targets in Gaza", set())
                 and "Libyan National Army" in app._detect_org_phrases("The Libyan National Army advanced on Tripoli", set())
                 and not any("government forces" in p.lower() for p in _det))  # lowercase 'forces' isn't a proper name
    ran[0] += 1
    if not _gloss_ok:
        fails.append(("glossary", "term-detect", "Houthis+IRGC, fair defs, no false-fire",
                      f"g1={_terms1} g2={_g2}",
                      "the story's groups are defined; ordinary words never trip a definition"))
    print(f"  {'ok ' if _gloss_ok else 'FAIL'} {_terms1}")

    # A ROUNDUP post's OWN photo can't be trusted to match the first-sentence headline we dot. SHIPPED BUG: a
    # Xi Jinping photo sat under an H-1B visa story (the post led with the visa item, its picture belonged to a
    # "Meanwhile, in China…" second item). Multi-topic -> drop source media; single-topic keeps it.
    print("\n=== ROUNDUP MEDIA GUARD (a multi-topic post's photo may not match our dot) ===")
    _rm_ok = (not app._post_media_trusted("Trump adds a $100k fee to H-1B visas. Meanwhile, Xi Jinping addressed the summit in Tianjin.")
              and not app._post_media_trusted("US restricts skilled-worker visas. In other news, China unveiled a new fighter jet.")
              and not app._post_media_trusted("Explosion rocks the port. Separately, the president signed a budget bill.")
              # GUARD: a single-topic post — even multi-sentence with a follow-up quote — keeps its own photo
              and app._post_media_trusted("Russia struck Kyiv overnight, killing three. Rescuers pulled survivors from the rubble.")
              and app._post_media_trusted("Lavrov said the talks collapsed. He added that Europe bears responsibility."))
    ran[0] += 1
    if not _rm_ok:
        fails.append(("srcmedia", "roundup-guard", "multi-topic drops photo; single-topic keeps it",
                      "see _post_media_trusted", "a post's own picture is trusted only when it's about one thing"))
    print(f"  {'ok ' if _rm_ok else 'FAIL'} roundup->drop, single-topic->keep")

    # An OFF-TOPIC report's text must never be folded in as a dot's paragraph. SHIPPED BUG: a "Pan-European
    # Stoxx 600 ends flat" markets line sat under a "US unveils Iran sanctions" headline. _shares_subject gates
    # the sum-fill in _absorb_source: fold only when the dup is actually about the headline.
    print("\n=== SUBJECT COHERENCE (an off-topic report can't become a dot's paragraph) ===")
    _sc_ok = (not app._shares_subject("US unveils expanded sanctions aimed at Iran economic asphyxiation",
                                      "Pan-European Stoxx 600 ends nearly flat as gains in travel and mining shares offset losses in energy stocks.")
              # GUARD: a genuine same-event report DOES share the subject -> still folds in
              and app._shares_subject("US unveils expanded sanctions aimed at Iran economic asphyxiation",
                                      "The US Treasury imposed fresh sanctions targeting Iran oil exports.")
              and app._shares_subject("Queensland bail breach laws to carry minimum 12 month jail sentence",
                                      "Premier David Crisafulli confirmed the bail legislation will apply to youths and adults."))
    ran[0] += 1
    if not _sc_ok:
        fails.append(("coherence", "shares-subject", "off-topic dropped; same-event kept",
                      "see _shares_subject", "a markets wrap never becomes the body of an Iran-sanctions dot"))
    print(f"  {'ok ' if _sc_ok else 'FAIL'} off-topic->drop, same-event->keep")

    # The SOURCE label is the reporting OUTLET, never a photo/wire CREDIT. SHIPPED BUG: a France24 story was
    # bylined "© Siddiqullah Alizai, AP" (the hero photo's credit), so "Read the original at © Siddiqullah
    # Alizai, AP" linked to france24.com — a name/link mismatch. A credit-looking name falls back to the domain.
    print("\n=== SOURCE NAME (a photo credit is not the outlet) ===")
    _src_ok = (app._clean_source_name("© Siddiqullah Alizai, AP", "france24.com") == "France 24"
               and app._clean_source_name("Kent Nishimura/AFP", "france24.com") == "France 24"
               and app._clean_source_name("File photo", "reuters.com") == "Reuters"
               and app._clean_source_name("", "france24.com") == "France 24"
               # GUARD: a real outlet name is kept as-is (incl. a bare wire like "AP")
               and app._clean_source_name("Reuters", "reuters.com") == "Reuters"
               and app._clean_source_name("AP", "apnews.com") == "AP"
               and app._clean_source_name("NOELREPORTS", "") == "NOELREPORTS")
    ran[0] += 1
    if not _src_ok:
        fails.append(("source", "clean-name", "photo credit -> outlet; real name kept",
                      "see _clean_source_name", "the byline must match the link the reader opens"))
    print(f"  {'ok ' if _src_ok else 'FAIL'} photo-credit->outlet, real-name->kept")

    # Telegram channels carry a factual, EVEN-HANDED lean note (pro-Ukraine AND pro-Russia by the same standard),
    # exactly like the state-media ownership notes; an aggregator with no documented lean stays unlabelled.
    print("\n=== CHANNEL LEAN (even-handed, only when documented) ===")
    _lean_ok = (app._source_note("NOELREPORTS", "t.me") == "Pro-Ukraine coverage"
                and app._source_note("Rybar", "t.me") == "Pro-Russia coverage"
                and app._source_note("Bellum Acta", "t.me") == ""          # no documented lean -> silent
                and app._source_note("TASS", "tass.ru") == "Russian state media"   # state media still wins first
                and app._source_note("France 24", "france24.com") == "French public broadcaster")
    ran[0] += 1
    if not _lean_ok:
        fails.append(("lean", "channel-note", "documented lean labelled; unknown silent; state-media unaffected",
                      "see _source_note/_TG_LEAN", "even-handed, never a guessed political label"))
    print(f"  {'ok ' if _lean_ok else 'FAIL'} NOELREPORTS=pro-UA, Rybar=pro-RU, unknown silent")

    # A wrongly-matched report's PICTURE must never become a dot's hero. SHIPPED BUG: a Dolly-Parton tribute
    # image sat atop a SpaceX story. Image transfers on merge/promotion are now subject-gated; a multi-topic
    # ROUNDUP post's own image is dropped too (its picture may belong to a later item than the headline).
    print("\n=== HERO IMAGE COHERENCE (a mismatched photo can't become the hero) ===")
    _p1 = {"title": "SpaceX unveils $100 billion Starbase Louisiana", "image": "", "sum": ""}
    app._absorb_source(_p1, {"title": "Dolly Parton, country legend, dead at 79",
                             "image": "https://x/dolly.jpg", "sum": "The singer died Tuesday."})
    _p2 = {"title": "SpaceX unveils $100 billion Starbase Louisiana", "image": "", "sum": ""}
    app._absorb_source(_p2, {"title": "SpaceX reveals huge new Starbase site in Louisiana",
                             "image": "https://x/starbase.jpg", "sum": "Starship flights planned."})
    _img_ok = (_p1.get("image") == ""                                   # off-topic image blocked
               and _p2.get("image") == "https://x/starbase.jpg"        # same-event image kept
               # a multi-topic roundup post's own image is not trusted; a single-topic post's is
               and not app._post_media_trusted("SpaceX unveils Starbase Louisiana. Meanwhile, Dolly Parton has died at 79.")
               and app._post_media_trusted("SpaceX unveils Starbase Louisiana with 30+ Starship flights a day."))
    ran[0] += 1
    if not _img_ok:
        fails.append(("hero-image", "coherence", "off-topic image blocked; same-event kept; roundup image dropped",
                      "see _absorb_source/_post_media_trusted", "a mismatched photo never becomes the hero"))
    print(f"  {'ok ' if _img_ok else 'FAIL'} off-topic->blocked, same-event->kept, roundup->dropped")

    total = (4 + len(CATEGORY_CASES) + len(GEO_CASES) + len(GEO_URL_CASES) + len(FLUFF_CASES) + len(SPORTS_WORTHY_CASES) + len(CLIMATE_WORTHY_CASES) + len(QUOTE_IMPORTANT_CASES)
             + len(DEDUP_CASES) + len(SIM_CASES) + len(FIPS_CASES) + len(CMATCH_CASES) + len(VER_CASES)
             + len(NAMEMATCH_CASES) + len(LEADER_PICK_CASES) + len(FB_PARSE_CASES) + len(LEAN_CASES)
             + len(SAME_PERSON_CASES) + len(DEAD_LEADER_CASES) + len(TG_CLEAN_CASES) + 1

             + len(CLIP_CASES) + len(HEADLINE_CASES) + len(DATELINE_CASES) + len(DATELINE_STRIP_CASES)
             + len(FLAG_CASES) + len(CSS_URL_CASES) + len(MEDIA_DEDUP_CASES)
             + len(CLEAN_HEADLINE_CASES) + len(COLLAPSE_CASES) + len(CLASSIFY_STRIKE_CASES)
             + len(CHATTER_CASES) + len(RELIABLE_CASES) + len(HARD_NEWS_CASES) + len(SHARPEN_CASES) + len(STANDALONE_CASES) + 1
             + 1   # + flag-coverage one-off
             + 1   # + allow_ai gate (cold-start build makes no live geo calls)
             + 1   # + summary cache is independent of _DATA_VER (a content bump serves the cached brief)
             + 1   # + cache janitor clears stale waste but spares live feed/location/dedup/leader files
             + 1   # + Gemini failover (capped primary -> backup) + preferred geo second opinion
             + 1   # + _wiki_thumb bounds Wikimedia URLs to a thumbnail
             + 1   # + map-worthy importance gate (broad-feature / local drop)
             + 3    # + casualty-fingerprint merge + AI semantic-dedup net + water-not-collapsed
             + 1    # + first-reporter promotion (inline dedup keeps whoever broke it as the primary)
             + 1    # + promotion pairs headline+body (no DPRK-headline-over-gasoline-body Frankenstein)
             + 2    # + text-sharpen (credit strip + end-stop) + who's-involved glossary detection
             + 45   # + self-learning gazetteer: 3 confidence + learned cold-start + nominatim learn + persist + wrong-country guard + 3 leader-statement->capital + 2 maritime-strike->water + 4 US-markets->NYC + 3 obituary-location + 2 Amur-complex + 2 Georgia-US-state + 2 desc-containment + 3 vessel-strike/threat + 3 asylum/flee + 2 passive-agent + tibet + vaca-muerta + envoy + company-wiki + 2 yucatan + 6 company-HQ (3 hire/exec/earnings->HQ + 3 guards: named-country, not-internal fine, named-scene)
             + 8    # + finish-brief (a summary never ends mid-sentence, incl. the "…, X says…" cutoff + dangling-preposition-before-period: 8 cases)
             + 1    # + port-profile json extractor
             + 1    # + port infobox facts parser
             + 1    # + facility word (airport) is not a place
             + 1    # + heads-of-state list overrides a stale head of government
             + 1    # + name-based dedup (a re-headlined copy is caught by shared names)
             + 1    # + roundup media guard (a multi-topic post's own photo is not trusted to match the dot)
             + 1    # + subject coherence (an off-topic report can't fill a dot's paragraph)
             + 1    # + source name (a photo credit is not the outlet; byline matches the link)
             + 1    # + channel lean (even-handed Telegram-channel bias note, only when documented)
             + 1    # + hero image coherence (a mismatched photo can't become the hero via merge/roundup)
             + 1)   # + Live TV video-id extraction + channel-list integrity (offline)
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