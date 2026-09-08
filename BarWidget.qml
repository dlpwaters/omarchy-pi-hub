import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.dlpwaters.pi-hub"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.settings = root.settings
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  Loader {
    id: panelLoader
    active: true
    visible: false
    source: Qt.resolvedUrl("Panel.qml")
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }
  IpcHandler {
    target: "io.github.dlpwaters.pi-hub"
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.toggle() }
    function refresh() { if (panelLoader.item) panelLoader.item.refresh() }
    function tab(name: string) { if (panelLoader.item) panelLoader.item.setTab(name) }
    function scope(value: string, project: string) { if (panelLoader.item && ["global", "project"].indexOf(value) >= 0) panelLoader.item.setScope(value, project) }
    function create(template: string) { if (panelLoader.item && panelLoader.item.isLibrary) panelLoader.item.newResource(template) }
    function select(index: int) { if (panelLoader.item) panelLoader.item.choose(index) }
    function review(source: string) { if (panelLoader.item) panelLoader.item.reviewSource(source) }
    function focus(control: string) { if (panelLoader.item) panelLoader.item.focusControl(control) }
    function status(): string { return panelLoader.item ? panelLoader.item.status() : JSON.stringify({loaded: false}) }
  }
  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "π"
    active: panelLoader.item ? panelLoader.item.opened : false
    useActiveColor: true
    tooltipText: "Pi Hub · Extensions, skills & prompts"
    onPressed: root.toggle()
  }
}
