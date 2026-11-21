# v13 - is update me kya badla

## Admin panel: kharcha (cost) tracker
- "Estimated cost by feature": idle bot, /play streaming, radio streaming, song download, storage upload, play card, broadcast - aaj / is mahine / total (+ CPU ghante, RAM GB-ghante, egress GB).
- "Cost per song": har gaane ka stream minutes, egress MB, kharcha aur kharcha per play (sabse mahange pehle).
- Ye ANUMAAN hai (CPU + RAM + egress naap kar baanta jata hai), Railway ka invoice nahi. Railway > Usage se milao, fark ho to COST_SCALE badlo.
- Naya file costs.py, requirements me psutil, 2 naye DB table (cost_feature, cost_song). Dockerfile me costs.py COPY.
- Naye env (optional): COST_RAM_GB_MONTH, COST_CPU_VCPU_MONTH, COST_EGRESS_GB, AUDIO_KBPS, COST_SCALE (.env.example dekho).

## /file (sirf OWNER_ID)
- Owner ke private chat me 2 CSV aati hain: songs_played.csv (kaun sa gaana kitni baar baja) aur radio_ratings.csv (radio gaano ki likes / skips / rating).
- Group me chalao to bhi file DM me aati hai. Pehle bot ko private me ek baar /start karna zaroori. Command menu me nahi dikhta.

# v11 - is update me kya badla

## Play card (naya design)
- Gaane ki asli photo (YouTube thumbnail) background me blur hoti hai aur wahi photo beech me box me dikhti hai.
- Photo ke dono taraf equalizer bars, upar NOW PLAYING, gaane ka naam blue box me, progress bar, time, prev/pause/next, Requested by.
- Photo na mile (SoundCloud / download fail) to rangeen art tile + vinyl, card kabhi fail nahi hota.
- Card ke neeche ka text 2 quote box me (blue patti): "Started Streaming" aur Title / Duration / By (/ Rating). Reject ho to purana caption.

## Rangeen buttons
- Pause / Skip / Loop / Help / Support / Update / Open radio menu = blue, Resume / Add me / Open bot / Like = green, Stop = red.
- Library ya Telegram support na kare to normal buttons (kuch tootta nahi). .env: BUTTON_COLORS=0 se band.

## Voice chat: join / invite messages
- /vcjoin on|off (default on). /setup me status.
- Koi VC join kare: "X joined the voice chat" (VC_JOIN_DELETE_SEC = 10 sec baad delete). Sirf tab jab assistant VC me ho (gaana chal raha ho).
- Koi VC me invite kare: "A invited B to the voice chat" + Open bot button (VC_INVITE_DELETE_SEC = 30 sec baad delete).

# v9 - is update me kya badla

## 1000 group
- Assistant ki had ASSISTANT_MAX_GROUPS (450): naya group hamesha us assistant ko jisme jagah ho. 90% bharne par OWNER_ID ko alert.
- MAX_ACTIVE_GROUPS, MAX_WAITERS, AUDIO_QUALITY, TG_TRANSMISSIONS, TG_SLEEP_THRESHOLD (.env.example dekho).
- Telegram storage se gaane ek-ek nahi, 8 ek saath. Restart message 25-25 groups ke batch me.
- Panel: Assistants table me groups / capacity.
- Zaroori: 1000 group = kam se kam 3 assistant account. Ek saath kitne gaane baj sakte hain wo server CPU par depend karta hai.

## Search / play: sahi naam + kitni baar baja
- YouTube title saaf hota hai ("Tum Hi Ho (Official Video) | T-Series" -> "Tum Hi Ho"), wahi naam save hota hai.
- Pehchaan video id se: alag-alag search ek hi gaana ginte hain. Loop ka repeat nahi ginte.
- "Started streaming" me "Played: N times". Naya /top: sabse jyada baje gaane.
- Panel: "Songs played" table (naam, type, played, like, skip, rating).

## Radio
- /radio = automatic trending songs (help aur menu me likha hai).
- Skip = gaane ki rating -1, 👍 Like button = +1 (ek user ek gaane ko 20 minute baad dobara, LIKE_COOLDOWN_MIN). rating = likes - skips.
- Radio gaane rating ke hisaab se chune jate hain (+3 par chance double, -3 par aadha).
- Panel: best rated / worst rated radio gaane.

## Assistant ban / voice chat
- Assistant ban ho to saaf message: kaise unban karna hai. Unban hote hi gaana khud shuru. Dusra assistant ho to wo le leta hai.
- Voice chat chalu nahi ho to sirf "voice chat chalu karo" bolta hai, admin banane ko nahi (/setup me bhi).

## /help
- Ab hamesha jawab deta hai (HTML -> plain text -> normal message fallback). Help button bhi.
- Command menu (/help, /play ...) Telegram me apne aap set hota hai.


## Assistant / group join
- /setup : bot ke rights, assistant, voice chat check karke batata hai kya kami hai (sirf admins).
- Assistant add na ho paye to gaana yaad rakha jata hai. Assistant add hote hi (ya admin right milte hi) gaana khud shuru. Wait: RESUME_WAIT (default 300 sec).
- Group -> assistant mapping DB me save (restart ke baad duplicate assistant nahi aata).
- FloodWait / "too many groups" / dead session: dusra assistant use hota hai. Panel me "Assistants" table. OWNER_ID set karo to dead session par private alert.
- Assistant ban ho to group me message: "assistant BANNED, unban karo". Unban par bhi message. /setup me bhi dikhta hai.
- IDLE_LEAVE_DAYS (default 30): jis group me itne din gaana nahi baja, assistant nikal jata hai (agli /play par wapas aata hai). 0 = band.

## Notices
- Voice chat shuru / band hone par group me time ke saath message (TIMEZONE ke hisaab se). /vcnotice on|off
- Naya member aaye to private welcome (naam + group ka naam). Telegram ke niyam se DM sirf unko jaata hai jinhone bot ko /start kiya ho; baaki ko group me chhota welcome (60 sec baad delete). /welcome on|off, WELCOME_TEXT se text badal sakte ho.

## Speed
- tgcache lookups RAM me, hit count 1 minute me ek baar likha jata hai, seen() RAM me, SQLite PRAGMA, panel stats 20 sec cache.
- Storage channel upload: ek time par ek, beech me UPLOAD_GAP (3 sec), FloodWait par ek retry, backlog limit.

## Naya script
- rebuild_cache.py : DB kho jaye to storage channel se gaano ki list wapas banata hai (usi BOT_TOKEN se).

## Nayi env variables
OWNER_ID, IDLE_LEAVE_DAYS, RESUME_WAIT, VC_NOTICE_DEFAULT, WELCOME_DEFAULT, WELCOME_TEXT, UPLOAD_GAP, MAX_UPLOAD_BACKLOG (.env.example dekho)

## Radio: ab singer nahi, feeling (mood)
- Naya flow: /radio -> language -> feeling (Romantic / Sad / Chill / Party / Attitude / Dance / Melody...) -> gaane bajne lagte hain.
- Singer ka naam menu me aur gaane ke title me kahin nahi dikhta (sirf YouTube search ko sahi gaana dhoondhne me madad ke liye andar use hota hai).
- Languages: Hindi, Punjabi, Haryanvi, Bhojpuri, Tamil, English. Har language me "Mix" bhi hai.
- songs.csv ka naya format: title,artist,language,mood  (mood "Romantic|Sad" = dono feelings me aata hai).
- Bhojpuri me 🔞 18+ feeling bhi hai (137 gaane). Chhupana ho to .env me HIDDEN_MOODS=18+ lagao.
- Punjabi: duplicate / remix / lofi / skit / intro hata diye (1292 -> ~955 gaane) aur har gaane ko feeling di gayi.

## /start aur /help group me
- /start ab group me bhi chalta hai (start screen + Help button). /help group aur private dono me chalta hai.

## Admin panel: radio stats
- Panel me "Radio - language plays": har language kitni baar start hui, kitne gaane baje, aaj aur is mahine kitne. Neeche language + feeling ki alag table. Sirf ginti save hoti hai (kaun/kis group ka record nahi). DB_PATH volume par ho to restart ke baad bhi bachta hai.

## Play card
- Ab /play par colourful card (image) aata hai: har gaane ka alag rang, vinyl, equalizer. Card na bane to purana text message.
- Naya file card.py, requirements me pillow, Dockerfile me font. Termux update me card.py bhi copy karna.
- 'Played: N times' card se hata diya. /top command hata diya (help + menu se bhi).
- Queue/radio khatm hone par aur VC start/end message me ab ye bhi: /play - play a song, /radio - automatic trending songs.

## Railway Hobby ($5) par 500+ group
- Variables: ASSISTANT_MAX_GROUPS=450, SESSION_STRINGS = kam se kam 2 assistant (500+ group = 2 account), MAX_ACTIVE_GROUPS=200, AUDIO_QUALITY=MEDIUM, DB_PATH=/data/bot.db (Railway Volume /data par mount zaroori, warna redeploy par data ud jata hai).
- 500 group ka matlab 500 group bot me hain; ek saath gaana bajne wale group kam hote hain. Har chalta gaana ek ffmpeg process = CPU + RAM. Railway usage-based hai, $5 credit se zyada use hua to bill badhega: Railway me Usage limit lagao.
- MAX_ACTIVE_GROUPS ka default 1000 se 200 kiya (env se badal sakte ho). Railway Metrics me CPU/RAM dekh kar badhao.
