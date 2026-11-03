const traceStages = [
  {
    phase: "PLAN / 01",
    name: "stage.refining",
    description:
      "OpenClaw turns the issue into a plan with acceptance criteria. The engine records the handoff and waits for approval when that gate is enabled.",
    component: "OpenClaw planner",
    child: "agent.<driver>.planner",
    evidence: "Plan, questions, approval state",
    note: "Supplied prompts and final responses require opt-in content capture.",
  },
  {
    phase: "BUILD / 02",
    name: "stage.implementing",
    description:
      "A coding CLI writes tests and code in an isolated worktree. Agent routing and exposed CLI activity are attached to this stage.",
    component: "Claude Code, Codex, or Cursor",
    child: "routing.selected",
    evidence: "Test design, code changes, agent response",
    note: "Exposed tool calls can be captured; hidden provider requests are not available.",
  },
  {
    phase: "VERIFY / 03",
    name: "stage.gates",
    description:
      "Devflow runs configured checks in a separate checkout and records each gate result before review can pass.",
    component: "Devflow test engine",
    child: "gate.result",
    evidence: "Gate type, exit status, output",
    note: "Gate outcomes appear in metadata mode. Commands and output require opt-in content capture.",
  },
  {
    phase: "REVIEW / 04",
    name: "stage.in_review",
    description:
      "A reviewer checks the diff and test evidence. Blocking findings return the ticket to the coding agent.",
    component: "Reviewer + Devflow engine",
    child: "agent.<driver>.reviewer",
    evidence: "Verdict, findings, reviewed SHA",
    note: "A review round can repeat build, gates, and review within the ticket session.",
  },
  {
    phase: "MERGE / 05",
    name: "stage.awaiting_merge_approval",
    description:
      "The engine checks PR approval, CI, and the remote head before merging the exact reviewed commit.",
    component: "GitHub PR + Devflow merge gate",
    child: "tool.github",
    evidence: "CI result, reviewed SHA, merge result",
    note: "With the approval gate on, this stage waits for an authorized PR comment.",
  },
];
const graphNodes = [...document.querySelectorAll("[data-graph-step]")];
const stageButtons = [...document.querySelectorAll("[data-stage]")];
const traceButtons = [...document.querySelectorAll("[data-trace-step]")];
const panels = [...document.querySelectorAll("[data-panel]")];
const edges = [...document.querySelectorAll("[data-edge]")];
const playControl = document.querySelector("#play-control");
const meter = document.querySelector("#stage-meter-fill");
const inspector = document.querySelector(".trace-inspector");
const stageViewport = document.querySelector(".stage-viewport");
const traceOutput = {
  phase: document.querySelector("#trace-phase"),
  name: document.querySelector("#trace-name"),
  description: document.querySelector("#trace-description"),
  component: document.querySelector("#trace-component"),
  child: document.querySelector("#trace-child"),
  evidence: document.querySelector("#trace-evidence"),
  note: document.querySelector("#trace-note"),
};
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let current = 0;
let playing = !reducedMotion.matches;
let timer;
function render(index) {
  current = index;
  graphNodes.forEach((node, position) =>
    position === index
      ? node.setAttribute("aria-current", "step")
      : node.removeAttribute("aria-current"),
  );
  stageButtons.forEach((button, position) =>
    position === index
      ? button.setAttribute("aria-current", "step")
      : button.removeAttribute("aria-current"),
  );
  traceButtons.forEach((button, position) =>
    position === index
      ? button.setAttribute("aria-current", "true")
      : button.removeAttribute("aria-current"),
  );
  panels.forEach((panel, position) => {
    panel.hidden = position !== index;
  });
  edges.forEach((edge, position) =>
    edge.classList.toggle(
      "is-active",
      position === Math.min(index, edges.length - 1),
    ),
  );
  meter.style.width = `${(index + 1) * 20}%`;
  for (const [key, element] of Object.entries(traceOutput))
    element.textContent = traceStages[index][key];
  inspector.classList.remove("trace-enter");
  void inspector.offsetWidth;
  inspector.classList.add("trace-enter");
}
function stop() {
  clearInterval(timer);
  timer = undefined;
}
function syncControl() {
  playControl.textContent = playing ? "Pause Ⅱ" : "Play ▷";
  playControl.setAttribute(
    "aria-label",
    playing ? "Pause stage animation" : "Play stage animation",
  );
}
function start() {
  stop();
  if (playing && !document.hidden)
    timer = setInterval(() => render((current + 1) % panels.length), 4200);
  syncControl();
  stageViewport.setAttribute("aria-live", playing ? "off" : "polite");
  document.body.classList.toggle("is-paused", !playing);
}
for (const button of [...graphNodes, ...stageButtons, ...traceButtons]) {
  button.addEventListener("click", () => {
    stageViewport.setAttribute("aria-live", "polite");
    render(
      Number(
        button.dataset.graphStep ??
          button.dataset.stage ??
          button.dataset.traceStep,
      ),
    );
    playing = false;
    start();
  });
}
playControl.addEventListener("click", () => {
  playing = !playing;
  start();
});
document.addEventListener("visibilitychange", start);
reducedMotion.addEventListener("change", () => {
  playing = false;
  start();
});
render(0);
start();
