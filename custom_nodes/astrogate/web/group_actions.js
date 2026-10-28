import { app } from "../../scripts/app.js";

function groupSelection() {
    const canvas = app.canvas;
    const graph = canvas.graph;
    const selected = [...canvas.selectedItems].filter(item => graph.nodes.includes(item) || graph.groups.includes(item));
    if (!selected.length) return false;
    const group = new LiteGraph.LGraphGroup("新分组");
    group.resizeTo(selected, 24);
    graph.beforeChange();
    graph.add(group);
    group.recomputeInsideNodes();
    canvas.selectItems([group]);
    graph.afterChange();
    canvas.setDirty(true, true);
    return true;
}

function ungroupSelection() {
    const canvas = app.canvas;
    const graph = canvas.graph;
    const groups = [...canvas.selectedItems].filter(item => graph.groups.includes(item));
    if (!groups.length) return false;
    for (const group of groups) group.recomputeInsideNodes();
    const outer = groups.filter(group => !groups.some(parent => parent !== group && parent.children.has(group)));
    const nodes = [...new Set(outer.flatMap(group => group.nodes))];
    graph.beforeChange();
    for (const group of outer) graph.remove(group);
    canvas.selectItems(nodes);
    graph.afterChange();
    canvas.setDirty(true, true);
    return true;
}

function installButtons() {
    const bar = document.querySelector(".ag-native-toolbar");
    if (!bar) return;
    for (const [label, hint, action] of [
        ["框选成组", "先框选节点，再点击成组；拖动分组标题可一起移动", groupSelection],
        ["拆分组", "选中分组后拆分；节点和连线保留", ungroupSelection],
    ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.title = hint;
        button.addEventListener("click", () => {
            if (!action()) {
                button.textContent = label === "框选成组" ? "请先框选节点" : "请先选中组";
                setTimeout(() => button.textContent = label, 2500);
            }
        });
        bar.append(button);
    }
}

app.registerExtension({
    name: "AstroGate.GroupActions",
    setup() { queueMicrotask(installButtons); },
    getCanvasMenuItems() {
        return [{content: "框选节点成组", callback: groupSelection}, {content: "拆分选中组", callback: ungroupSelection}];
    },
});
