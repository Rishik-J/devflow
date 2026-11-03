const stages = [
  { status: 'Planning', actor: 'OpenClaw · planner', title: 'A plan before code', text: 'The issue becomes a concrete implementation and test plan. A configured approver reviews it before work begins.', evidence: 'Artifact: issue plan' },
  { status: 'Building', actor: 'Coding agent · worktree', title: 'Changes in isolation', text: 'A coding agent writes tests and implementation in a separate git worktree, with the issue and repository context in hand.', evidence: 'Artifact: code + tests' },
  { status: 'Verifying', actor: 'Devflow · test gates', title: 'Run the real checks', text: 'Configured test gates run in a separate checkout. Failures are recorded as evidence and sent back for repair.', evidence: 'Artifact: gate results' },
  { status: 'Reviewing', actor: 'Review agent · diff', title: 'An independent pass', text: 'A reviewer examines the diff and test evidence. Findings return to the coder for another pass.', evidence: 'Artifact: review findings' },
  { status: 'Ready to merge', actor: 'Devflow · merge gate', title: 'The reviewed commit', text: 'After PR approval and CI checks, Devflow merges the exact commit that passed review and records what the run taught it.', evidence: 'Artifact: verified PR' }
];
const buttons = [...document.querySelectorAll('[data-step]')];
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
buttons.forEach(button => button.addEventListener('click', () => {
  render(Number(button.dataset.step));
  playing = false;
  stop();
  syncToggle();
}));
toggle.addEventListener('click', () => { playing = !playing; start(); });
document.addEventListener('visibilitychange', start);
reducedMotion.addEventListener('change', () => { playing = false; stop(); syncToggle(); });
start();
