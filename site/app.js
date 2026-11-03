const stages = [
  { status: 'Planning', actor: 'OpenClaw · planner', title: 'A plan before code', text: 'The issue becomes a concrete implementation and test plan. A configured approver reviews it before work begins.', evidence: 'Artifact: issue plan' },
  { status: 'Building', actor: 'Coding agent · worktree', title: 'Changes in isolation', text: 'A coding agent writes tests and implementation in a separate git worktree, with the issue and repository context in hand.', evidence: 'Artifact: code + tests' },
  { status: 'Verifying', actor: 'Devflow · test gates', title: 'Run the real checks', text: 'Configured test gates run in a separate checkout. Failures are recorded as evidence and sent back for repair.', evidence: 'Artifact: gate results' },
  { status: 'Reviewing', actor: 'Review agent · diff', title: 'An independent pass', text: 'A reviewer examines the diff and test evidence. Findings return to the coder for another pass.', evidence: 'Artifact: review findings' },
  { status: 'Ready to merge', actor: 'Devflow · merge gate', title: 'The reviewed commit', text: 'After PR approval and CI checks, Devflow merges the exact commit that passed review and records what the run taught it.', evidence: 'Artifact: verified PR' }
];
const traceStages = [
  { phase: 'PLAN / 01', title: 'stage.refining', description: 'OpenClaw turns the issue into a plan with acceptance criteria. The engine records the handoff and waits for plan approval when that gate is enabled.', component: 'OpenClaw planner', child: 'agent.<driver>.planner', artifact: 'Plan, questions, approval state', note: 'The supplied prompt and final response are visible only when content capture is enabled.' },
  { phase: 'BUILD / 02', title: 'stage.implementing', description: 'A coding agent writes tests and code in an isolated worktree. Agent routing and exposed CLI activity are attached to this work.', component: 'Claude Code, Codex, or Cursor', child: 'routing.selected', artifact: 'Test design, code changes, agent response', note: 'Exposed tool calls and final responses can be captured; hidden provider requests are not available.' },
  { phase: 'VERIFY / 03', title: 'stage.gates', description: 'Devflow runs the configured checks in a separate checkout and records each gate outcome before a review can pass.', component: 'Devflow test engine', child: 'gate.result', artifact: 'Gate type, exit status, output', note: 'Gate commands and output require opt-in content capture. The gate outcome is available in metadata mode.' },
  { phase: 'REVIEW / 04', title: 'stage.in_review', description: 'A reviewer checks the diff and test evidence. Blocking findings return to the coding agent for another pass.', component: 'Review agent + Devflow engine', child: 'agent.<driver>.reviewer', artifact: 'Review verdict, findings, reviewed SHA', note: 'A review loop can repeat build, gates, and review spans within the ticket session.' },
  { phase: 'MERGE / 05', title: 'stage.awaiting_merge_approval', description: 'The engine checks PR approval, CI, and the remote head before merging the exact commit the reviewer approved.', component: 'GitHub PR + Devflow merge gate', child: 'tool.github', artifact: 'CI result, reviewed SHA, merge result', note: 'When the approval gate is on, this stage waits for an authorized PR comment.' }
];
const buttons = [...document.querySelectorAll('[data-step]')];
const flowButtons = [...document.querySelectorAll('[data-flow-step]')];
const traceButtons = [...document.querySelectorAll('[data-trace-step]')];
const traceOutput = {
  phase: document.querySelector('#trace-phase'), title: document.querySelector('#trace-title'),
  description: document.querySelector('#trace-description'), component: document.querySelector('#trace-component'),
  child: document.querySelector('#trace-child'), artifact: document.querySelector('#trace-artifact'),
  note: document.querySelector('#trace-note')
};
const inspector = document.querySelector('.trace-inspector');
const output = {
  status: document.querySelector('#demo-status'), actor: document.querySelector('#demo-actor'),
  title: document.querySelector('#demo-title'), text: document.querySelector('#demo-text'),
  evidence: document.querySelector('#demo-evidence'), icon: document.querySelector('#demo-icon')
};
const toggle = document.querySelector('#demo-toggle');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let current = 0;
let timer;
let playing = !reducedMotion.matches;
function render(index) {
  current = index;
  const stage = stages[index];
  for (const [key, element] of Object.entries(output)) element.textContent = key === 'icon' ? String(index + 1).padStart(2, '0') : stage[key];
  buttons.forEach((button, position) => {
    if (position === index) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  flowButtons.forEach((button, position) => {
    if (position === index) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  traceButtons.forEach((button, position) => {
    if (position === index) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  });
  for (const [key, element] of Object.entries(traceOutput)) element.textContent = traceStages[index][key];
  inspector.classList.remove('trace-enter');
  void inspector.offsetWidth;
  inspector.classList.add('trace-enter');
}
function stop() { clearInterval(timer); timer = undefined; }
function syncToggle() {
  toggle.innerHTML = playing ? 'Pause <span aria-hidden="true">Ⅱ</span>' : 'Play <span aria-hidden="true">▷</span>';
  toggle.setAttribute('aria-label', playing ? 'Pause walkthrough' : 'Play walkthrough');
}
function start() {
  stop();
  if (playing && !document.hidden) timer = setInterval(() => render((current + 1) % stages.length), 3500);
  syncToggle();
}
[...buttons, ...flowButtons, ...traceButtons].forEach(button => button.addEventListener('click', () => {
  render(Number(button.dataset.step ?? button.dataset.flowStep ?? button.dataset.traceStep));
  playing = false;
  stop();
  syncToggle();
}));
toggle.addEventListener('click', () => { playing = !playing; start(); });
document.addEventListener('visibilitychange', start);
reducedMotion.addEventListener('change', () => { playing = false; stop(); syncToggle(); });
start();
