const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '..', 'dashboard.js'), 'utf8');
const renderer = script.slice(script.indexOf('function tableFor('), script.indexOf('async function experimentAction('));
class Element {
  constructor(tag, value) { this.tag = tag; this.textContent = value ?? ''; this.children = []; this.dataset = {}; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
function setup(experiment, overrides = {}) {
  const elements = {};
  const players = [{id:'agent-1',name:'Learner',model:'fixture'}, {id:'agent-2',name:'Fixed opponent',model:'fixture'}];
  const context = vm.createContext({experimentState:{experiment}, experimentConnected:true, busy:false, experimentBusy:false, connected:true,
    experimentRenderKey:'', latest:{}, config:{players}, active:()=>false, modelLabel:p=>p.model, Node:Element,
    Option:class extends Element {constructor(text,value){super('option',text);this.value=value}},
    $:id=>elements[id] ||= new Element('div'), node:(tag,value)=>new Element(tag,value), ...overrides});
  vm.runInContext(renderer + '\nrenderExperiments();',context);
  return elements;
}
function experiment(phase='training') {
  const arm={completed:0,target:4,wins:0,losses:0,draws:0,win_rate:null,win_rate_interval:null,median_victory_seconds:null,invalid:0};
  return {id:'fixture',phase,scored_training:2,seed_checkpoint:null,active_run:null,
    report:{arms:{baseline:{...arm},learned:{...arm}},runs:[]},
    next:{arm:'training',players:[{id:'agent-1',name:'Learner'},{id:'agent-2',name:'Fixed'}],learner_id:'agent-1',strategy:'Sampled when started'}};
}
test('create uses ordinary roster and requires experiment connection',()=>{
  let dom=setup(null); assert.equal(dom['experiment-create'].disabled,false); assert.equal(dom['experiment-detail'].hidden,true);
  dom=setup(null,{experimentConnected:false}); assert.equal(dom['experiment-create'].disabled,true);
});
test('training exposes explicit freeze and prevents overlap',()=>{
  let dom=setup(experiment()); assert.equal(dom['experiment-freeze'].disabled,false); assert.equal(dom['experiment-setup'].hidden,true);
  assert.equal(dom['betting-panel'].hidden,true); assert.match(dom['experiment-next'].textContent,/fixed baseline opponent/);
  dom=setup(experiment(),{active:()=>true}); assert.equal(dom['experiment-run'].disabled,true); assert.equal(dom['experiment-freeze'].disabled,true);
  const empty=experiment(); empty.scored_training=0; dom=setup(empty); assert.equal(dom['experiment-freeze'].disabled,true);
});
test('frozen report shows true zeroes, win-only speed, exclusions, and recording links',()=>{
  const exp=experiment('evaluation'); exp.frozen_version='frozen-controller';
  exp.report.arms.baseline={completed:4,target:4,wins:0,losses:3,draws:1,win_rate:0,win_rate_interval:[0,.49],median_victory_seconds:null,invalid:1};
  exp.report.runs=[{id:'run-1',arm:'baseline',learner_id:'agent-2',slot:1,valid:false,reason:'Provider <failure>',elapsed_seconds:null,controller_version:'frozen-controller'}];
  const dom=setup(exp);
  assert.equal(dom['experiment-freeze'].hidden,true); assert.equal(dom['experiment-run'].textContent,'Run next evaluation');
  const cells=dom['experiment-report'].children[0].children[1].children[0].children;
  assert.equal(cells[3].textContent,'0.0%'); assert.equal(cells[5].textContent,'— (0 wins)');
  const row=dom['experiment-runs'].children[0].children[1].children[0].children;
  assert.equal(row[0].children[0].href,'/api/experiments/recording?match_id=run-1');
  assert.equal(row[3].textContent,'Excluded: Provider <failure>');
});
test('pending persistence or completed evaluation cannot launch',()=>{
  let exp=experiment(); let dom=setup(exp,{experimentState:{experiment:exp,pending:true,sync_message:'Retry pending'}});
  assert.equal(dom['experiment-run'].disabled,true); assert.match(dom['experiment-status'].textContent,/Retry pending/);
  exp=experiment('complete'); exp.next=null; dom=setup(exp);
  assert.equal(dom['experiment-run'].disabled,true); assert.equal(dom['experiment-end'].disabled,true); assert.equal(dom['experiment-create'].disabled,false);
});
