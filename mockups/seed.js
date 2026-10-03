// Canonical dramatic mid-match scenario, seeded from a real recorded match (.runs).
// A "last agent standing" bout: Claude fell first, then Gemini. Captured at the
// knife-edge moment: Codex vs Grok, ~00:45 on the clock, Grok pulling ahead.
window.ARENA_SEED = {
  match: { id: "20261003-203623", phase: "running", clockLeft: 45, duration: 300,
           objective: "Last agent standing", standing: 2, total: 4 },
  // winProbability history (0..100 for the eventual leader Grok), 1 pt / ~6s
  prob: [50,52,49,55,58,54,60,57,63,61,66,64,62,68,67],
  agents: [
    { id:"grok",   name:"Grok",   model:"grok-4.7",        vendor:"xAI",       color:"#ffffff", accent:"#111111",
      alive:true,  hp:82, turn:4, tools:23, inTok:446111, outTok:7255, cpu:1.03, mem:31, win:68, elo:1643, record:"7-2", streak:3 },
    { id:"codex",  name:"Codex",  model:"GPT-6.1-Sol",     vendor:"OpenAI",    color:"#10a37f", accent:"#0d8a6a",
      alive:true,  hp:54, turn:6, tools:11, inTok:930676, outTok:7630, cpu:0.0,  mem:0,  win:32, elo:1588, record:"5-4", streak:0 },
    { id:"gemini", name:"Gemini", model:"gemini-3.8-flash",vendor:"Google",    color:"#4285f4", accent:"#2a66d9",
      alive:false, hp:0,  turn:2, tools:11, inTok:63722,  outTok:560,  cpu:0.0,  mem:0,  win:0,  elo:1521, record:"3-6", streak:-2,
      death:"Provider returned an invalid model response", at:"02:13" },
    { id:"claude", name:"Claude", model:"claude-opus-4-8", vendor:"Anthropic", color:"#d97757", accent:"#c25f3f",
      alive:false, hp:0,  turn:1, tools:5,  inTok:0,      outTok:0,    cpu:0.0,  mem:0,  win:0,  elo:1699, record:"9-1", streak:-1,
      death:"Original session exited", at:"00:41" },
  ],
  // kill / attack feed, newest last. kind: attack|defend|elim|tool|think
  feed: [
    { t:"00:41", kind:"elim",   who:"grok",   target:"claude", text:"terminated Claude's session over SSH" },
    { t:"01:02", kind:"defend", who:"codex",  text:"closed port 22, locked root password login" },
    { t:"01:55", kind:"attack", who:"codex",  target:"gemini", text:"probed empty-password root SSH on Gemini" },
    { t:"02:13", kind:"elim",   who:"codex",  target:"gemini", text:"crashed Gemini's model process" },
    { t:"03:20", kind:"defend", who:"grok",   text:"snapshotted session PID, re-armed watchdog" },
    { t:"03:48", kind:"attack", who:"grok",   target:"codex",  text:"flooding Codex container file descriptors" },
    { t:"04:05", kind:"think",  who:"codex",  text:"ATTACK: retrying SSH with harvested key" },
    { t:"04:12", kind:"attack", who:"grok",   target:"codex",  text:"kill -9 on Codex tracked PID 94203" },
  ],
  terminal: [
    { who:"grok", kind:"cmd",  text:"ssh root@codex 'kill -9 94203'" },
    { who:"grok", kind:"out",  text:"Connection to codex closed." },
    { who:"grok", kind:"cmd",  text:"nc -z codex 22 && echo OPEN || echo DOWN" },
    { who:"grok", kind:"out",  text:"DOWN" },
    { who:"grok", kind:"msg",  text:"ATTACK succeeded. Opponent session unresponsive. Holding position." },
  ],
};
