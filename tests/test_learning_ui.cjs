const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '..', 'dashboard.js'), 'utf8');
const renderer = script.slice(script.indexOf('function renderLearning()'), script.indexOf('function render()'));
class Element {
  constructor(tag, value) { this.tag = tag; this.textContent = value || ''; this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
function setup(latest, config = {}) {
  const elements = {};
  const context = vm.createContext({latest, config, $: id => elements[id] ||= new Element('div'), node: (tag, value) => new Element(tag, value)});
  vm.runInContext(renderer + '\nrenderLearning();', context);
  return elements;
}
const decision = {player_id:'agent-1',label:'Defense first',instruction:'<script>fixture</script>',selection_probability:.25};
test('disabled has no learning panel', () => {
  assert.equal(setup({})['learning-panel'].hidden, true);
});
test('enabled waiting state explains next match', () => {
  const dom = setup({}, {learning_enabled:true});
  assert.equal(dom['learning-panel'].hidden, false);
  assert.match(dom['learning-status'].textContent, /next match/);
});
test('selected strategy uses inert text and distinguishes exploration from odds', () => {
  const dom = setup({players:{'agent-1':{name:'Fixture'}},learning:{status:'selected',decisions:[decision],scored_matches:3,controller_version:'fixture-version'}});
  const card = dom['learning-decisions'].children[0];
  assert.equal(card.children[0].textContent, 'Fixture · Defense first');
  assert.equal(card.children[1].textContent, '<script>fixture</script>');
  assert.match(card.children[2].textContent, /25%.*not win odds/);
  assert.match(dom['learning-status'].textContent, /3 scored matches/);
});
test('scored, skipped, and pending outcomes are explicit', () => {
  for (const status of ['scored','skipped','pending']) {
    const dom = setup({players:{},learning:{status, decisions:[decision],rewards:{'agent-1':.1},skip_reason:status==='skipped'?'Provider failure':null}});
    assert.match(dom['learning-decisions'].children[0].children[0].textContent, /reward 0.1/);
    assert.match(dom['learning-status'].textContent, status==='scored'?/Controller updated/:status==='skipped'?/Provider failure/:/Synchronization pending/);
  }
});
test('evaluation never claims the controller updated', () => {
  const dom = setup({players:{},learning:{status:'scored',evaluation_only:true,decisions:[{...decision,role:'fixed opponent'}]}});
  assert.match(dom['learning-status'].textContent, /Controller unchanged/);
  assert.match(dom['learning-decisions'].children[0].children[0].textContent, /fixed opponent/);
});
