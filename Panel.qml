import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import QtCore
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.dlpwaters.pi-hub"
  ipcTarget: moduleName
  manageIpc: false
  property var anchorItem: null
  property var hostWidget: null
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color muted: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.62)
  readonly property color subtle: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.10)
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl("hub.py")).replace(/^file:\/\//, ""))
  property string tab: "Browse"
  property string scope: "global"
  property string project: Quickshell.env("HOME")
  property string scopePath: ""
  property bool scopeReady: false
  property var packages: []
  property var resources: []
  property var results: []
  property var rows: []
  property int total: 0
  property int offset: 0
  property bool searched: false
  property bool piAvailable: false
  property bool busy: false
  property string action: ""
  property string request: ""
  property string message: ""
  property bool error: false
  property var selected: null
  property var review: null
  property string reviewFile: ""
  property bool acceptedReview: false
  property var resource: null
  property bool editing: false
  property string originalBody: ""
  property string originalName: ""
  property var pendingNavigation: null
  property var confirmation: null
  property string undoToken: ""
  readonly property bool isLibrary: tab === "Extensions" || tab === "Skills" || tab === "Prompts"
  readonly property string kind: tab.toLowerCase()
  readonly property bool dirty: editing && (bodyField.text !== originalBody || nameField.text !== originalName)
  readonly property bool editable: editing && (!resource || resource.editable === true)

  Settings {
    id: preferences
    location: "file://" + Quickshell.env("HOME") + "/.config/omarchy/pi-hub.ini"
    property string projectFolder: Quickshell.env("HOME")
    property string activeScope: "global"
  }
  Component.onCompleted: { project = preferences.projectFolder; scope = preferences.activeScope }

  function status() {
    return JSON.stringify({loaded: true, opened: opened, tab: tab, scope: scope, project: project,
      scopePath: scopePath, busy: busy, packages: packages.length, resources: resources.length,
      rows: rows.length, total: total, selected: selected ? selected.source || selected.path : "",
      review: review ? review.source : "", acceptedReview: acceptedReview, confirming: confirmation !== null, reviewFiles: review ? (review.files || []).length : 0,
      editing: editing, dirty: dirty, name: nameField.text, bodyLength: bodyField.length, path: resource ? resource.path : "", message: message, error: error})
  }
  function open() { controller.show(); if (!busy && !dirty) refresh(); Qt.callLater(function() { searchField.forceActiveFocus() }) }
  function close() { controller.hide() }
  function toggle() { opened ? close() : open() }
  function tell(text, failed) { message = text; error = failed === true }
  function navigate(callback) {
    if (busy) return
    if (dirty) { pendingNavigation = callback; confirmation = null; return }
    callback()
  }
  function clearSelection() {
    selected = null; review = null; reviewFile = ""; acceptedReview = false
    resource = null; editing = false; originalBody = ""; originalName = ""
    nameField.text = ""; bodyField.text = ""; confirmation = null
  }
  function setTab(value) {
    if (["Browse", "Installed", "Extensions", "Skills", "Prompts"].indexOf(value) < 0) return
    navigate(function() { tab = value; clearSelection(); searchField.text = ""; rebuild(); if (value === "Browse" && !searched) searchPackages(0) })
  }
  function setScope(value, folder) {
    navigate(function() {
      scopeReady = false; scopePath = ""
      scope = value; project = folder || project; preferences.activeScope = scope; preferences.projectFolder = project
      clearSelection(); refresh()
    })
  }
  function rebuild() {
    var query = searchField.text.trim().toLowerCase()
    if (tab === "Browse") { rows = results; return }
    var all = tab === "Installed" ? packages : resources.filter(function(r) { return r.kind === kind })
    rows = all.filter(function(r) { return !query || (r.name + " " + (r.description || "") + " " + (r.source || r.path || "")).toLowerCase().indexOf(query) >= 0 })
  }
  function refresh() { if (!busy) send({action: "list"}) }
  function searchPackages(page) { if (!busy) { offset = page; send({action: "search", query: searchField.text.trim(), offset: page}) } }
  function selectRow(row) {
    navigate(function() {
      clearSelection(); selected = row
      if (isLibrary) send({action: "read", path: row.path})
      else if (tab === "Browse") send({action: "review", source: row.source})
    })
  }
  function choose(index) { if (index >= 0 && index < rows.length) selectRow(rows[index]) }
  function reviewSource(source) {
    if (!source.trim()) { tell("Enter a package source to review.", true); return }
    navigate(function() { clearSelection(); selected = {source: source, name: source}; send({action: "review", source: source}) })
  }
  function loadResource(value) {
    resource = value; editing = true; originalName = value.name; originalBody = value.body
    nameField.text = value.name; bodyField.text = value.body; bodyScroll.contentItem.contentY = 0
  }
  function newResource(template) {
    navigate(function() {
      clearSelection(); editing = true
      send({action: "scaffold", kind: kind, template: template, name: "my-" + (kind === "skills" ? "skill" : kind === "prompts" ? "prompt" : "extension"), description: "Describe when to use this " + (kind === "skills" ? "skill" : kind === "prompts" ? "prompt" : "extension")})
    })
  }
  function promptStarter(template) {
    navigate(function() {
      clearSelection(); editing = true
      var starters = {
        review: ["review", "Review code for concrete defects", "Review $1 for correctness, security, and missing behavior checks. Focus on $@. Explain each concrete issue with a file reference and a suggested fix."],
        debug: ["debug", "Diagnose a failure from evidence", "Investigate this failure: $@. Inspect the failing stage and relevant logs first. Form a testable hypothesis, make the smallest justified fix, and verify the original failure no longer occurs."],
        docs: ["document", "Write practical project documentation", "Document $1 for a new user. Explain what it does, prerequisites, setup, usage examples, and troubleshooting grounded in the actual code. Additional context: $@."]
      }
      var value = starters[template]
      if (!value) return
      nameField.text = value[0]
      bodyField.text = "---\ndescription: " + value[1] + "\n---\n\n" + value[2] + "\n"
    })
  }
  function save() {
    if (!scopeReady || !editable || busy || !nameField.text.trim() || !bodyField.text.trim()) return
    if (!resource && kind === "skills") bodyField.text = bodyField.text.replace(/^name:.*$/m, "name: " + nameField.text.trim())
    send({action: "save", kind: kind, name: nameField.text.trim(), path: resource ? resource.path : "", body: bodyField.text, revision: resource ? resource.revision : ""})
  }
  function confirm(text, payload) { confirmation = {text: text, payload: payload}; pendingNavigation = null }
  function handleKey(event) {
    if (event.modifiers !== Qt.ControlModifier) return
    if (event.key === Qt.Key_S) save()
    else if (event.key === Qt.Key_F) searchField.forceActiveFocus()
    else if (event.key === Qt.Key_N && isLibrary) newResource(kind === "extensions" ? "command" : kind === "skills" ? "skill" : "prompt")
    else return
    event.accepted = true
  }
  function send(payload) {
    if (busy) return
    if (["install", "remove", "toggle", "toggleResource", "save", "delete", "restore"].indexOf(payload.action) >= 0 && !scopeReady) { tell("Choose a valid scope and refresh before making changes.", true); return }
    payload.scope = scope; payload.project = project
    action = payload.action; request = JSON.stringify(payload) + "\n"; busy = true; error = false
    tell(({search: "Searching the package catalog…", review: "Fetching package files for review…", reviewUpdate: "Checking the latest version…", install: "Installing the reviewed package…", list: "Reading your Pi resources…", save: "Saving…"})[action] || "Working…", false)
    worker.stdinEnabled = true; worker.running = true
  }
  function finish(code, output) {
    busy = false
    var result
    try { result = JSON.parse(output) } catch (e) { result = {ok: false, error: "Pi Hub could not read the helper response. Check the helper and Python installation."} }
    if (code !== 0 || !result.ok) { if (action === "list") scopeReady = false; tell(result.error || "The action failed.", true); return }
    tell(result.message || "", false)
    if (result.packages !== undefined) { packages = result.packages; resources = result.resources; scopePath = result.scopePath; scopeReady = true; piAvailable = result.piAvailable; rebuild() }
    if (action === "search") { results = result.results; total = result.total; offset = result.offset; searched = true; rebuild() }
    if (result.review) { review = result.review; reviewFile = ""; acceptedReview = false }
    if (action === "read" || action === "save") loadResource(result.resource)
    if (action === "scaffold") {
      originalName = ""; originalBody = ""
      nameField.text = "my-" + (kind === "skills" ? "skill" : kind === "prompts" ? "prompt" : "extension")
      bodyField.text = result.body; Qt.callLater(function() { nameField.forceActiveFocus(); nameField.selectAll() })
    }
    if (["save", "delete", "restore", "install", "remove", "toggle", "toggleResource"].indexOf(action) >= 0) {
      if (result.undoToken) undoToken = result.undoToken
      if (action === "restore") undoToken = ""
      if (action !== "save") clearSelection()
      var savedMessage = result.message || "Saved. In a running Pi session, use /reload to apply changes."
      afterRefreshMessage = savedMessage
      refresh()
    } else if (action === "list") {
      if (afterRefreshMessage) { tell(afterRefreshMessage, false); afterRefreshMessage = "" }
      if (pendingNavigation !== null && !dirty) { var next = pendingNavigation; pendingNavigation = null; next(); return }
      if (tab === "Browse" && !searched) searchPackages(0)
    }
  }
  property string afterRefreshMessage: ""
  Process {
    id: worker
    command: ["python3", root.helper, "rpc"]
    stdinEnabled: true
    onStarted: { write(root.request); stdinEnabled = false }
    stdout: StdioCollector { id: output; waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) { root.finish(code, output.text) }
  }
  Process {
    id: picker
    command: ["zenity", "--file-selection", "--directory", "--title=Pi Hub project folder", "--filename=" + root.project + "/"]
    stdout: StdioCollector { id: picked; waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(code) { root.controller.show(); if (code === 0 && picked.text.trim()) root.setScope("project", picked.text.trim()) }
  }
  Process {
    id: clipboard
    property string value: ""
    command: ["wl-copy"]
    stdinEnabled: true
    onStarted: { write(value); stdinEnabled = false }
    onExited: function(code) { root.tell(code === 0 ? "Copied. Paste into Pi; use /reload first after changes." : "Could not copy to the clipboard.", code !== 0) }
  }
  function focusControl(name) {
    var controls = {search: searchField, editor: bodyField, trust: trustButton, install: installButton, confirm: confirmAction, remove: removePackage}
    var control = controls[name]
    if (control && control.visible && control.enabled) control.forceActiveFocus()
  }
  function copy(text) { if (!clipboard.running) { clipboard.value = text; clipboard.stdinEnabled = true; clipboard.running = true } }

  component Label: Text {
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
  }
  component ActionButton: Button { focusable: true; bordered: true; enabled: !root.busy }

  KeyboardPanel {
    id: card
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: searchField
    contentWidth: fittedContentWidth(Style.space(1100))
    contentHeight: fittedContentHeight(Style.space(760))
    Item {
      anchors.fill: parent
      Keys.onEscapePressed: root.close()
      ColumnLayout {
        anchors.fill: parent
        spacing: Style.space(12)
        RowLayout {
          Label { text: "π"; color: root.accent; font.pixelSize: Style.space(34) }
          ColumnLayout {
            spacing: 2
            Label { text: "Pi Hub"; font.pixelSize: Style.font.title; font.bold: true }
            Label { text: "EXTENSIONS, SKILLS & PROMPTS"; color: root.muted; font.pixelSize: Style.font.caption; font.letterSpacing: 0.8 }
          }
          Item { Layout.fillWidth: true }
          ActionButton { text: "Refresh"; enabled: !root.busy && !root.dirty; onClicked: root.refresh() }
          Button { text: "Close"; focusable: true; onClicked: root.close() }
        }
        RowLayout {
          spacing: Style.space(5)
          Repeater {
            model: ["Browse", "Installed", "Extensions", "Skills", "Prompts"]
            ActionButton { required property string modelData; text: modelData; selected: root.tab === modelData; onClicked: root.setTab(modelData) }
          }
          Item { Layout.fillWidth: true }
          Label { visible: root.busy; text: "Working…"; color: root.accent; font.pixelSize: Style.font.caption }
        }
        Rectangle { Layout.fillWidth: true; height: 1; color: root.subtle }
        RowLayout {
          Label { text: "Apply to"; color: root.muted; font.pixelSize: Style.font.caption }
          ActionButton { text: "Global"; selected: root.scope === "global"; onClicked: root.setScope("global", root.project) }
          ActionButton { text: "Project"; selected: root.scope === "project"; onClicked: root.setScope("project", root.project) }
          TextField {
            id: projectField
            Layout.fillWidth: true
            visible: root.scope === "project"
            text: root.project
            placeholderText: "Absolute project folder"
            enabled: !root.busy
            onAccepted: root.setScope("project", text.trim())
            Keys.onPressed: function(event) { root.handleKey(event) }
          }
          ActionButton { visible: root.scope === "project"; text: "Use folder"; onClicked: root.setScope("project", projectField.text.trim()) }
          ActionButton { text: "Choose folder…"; enabled: !root.busy && !root.dirty && !picker.running; onClicked: { root.close(); picker.running = true } }
          Label { visible: root.scope === "global"; Layout.fillWidth: true; text: "Available across your Pi sessions"; color: root.muted; font.pixelSize: Style.font.caption }
        }
        Label {
          Layout.fillWidth: true
          text: root.scopePath || (root.scope === "project" ? "Choose the project folder before making changes." : "Loading Pi settings…")
          color: root.muted; font.pixelSize: Style.font.caption; elide: Text.ElideMiddle; wrapMode: Text.NoWrap
        }
        RowLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          spacing: Style.space(18)
          ColumnLayout {
            Layout.preferredWidth: Style.space(310)
            Layout.maximumWidth: card.contentWidth * 0.34
            Layout.fillHeight: true
            spacing: Style.space(9)
            TextField {
              id: searchField
              Layout.fillWidth: true
              placeholderText: root.tab === "Browse" ? "Search Pi packages…" : "Filter " + root.tab.toLowerCase() + "…"
              onTextChanged: if (root.tab !== "Browse") root.rebuild()
              onAccepted: if (root.tab === "Browse") root.searchPackages(0)
              Keys.onPressed: function(event) { root.handleKey(event) }
            }
            RowLayout {
              Layout.fillWidth: true
              visible: root.tab === "Browse"
              ActionButton { text: "Search catalog"; Layout.fillWidth: true; onClicked: root.searchPackages(0) }
              ActionButton { text: "↗"; tooltipText: "Open pi.dev package gallery"; onClicked: Qt.openUrlExternally("https://pi.dev/packages") }
            }
            ActionButton {
              visible: root.isLibrary
              Layout.fillWidth: true
              text: "New " + (root.tab === "Skills" ? "skill" : root.tab === "Prompts" ? "prompt" : "extension")
              onClicked: root.newResource(root.kind === "extensions" ? "command" : root.kind === "skills" ? "skill" : "prompt")
            }
            Label { text: root.tab === "Browse" ? root.total + " PACKAGES" : root.rows.length + " " + root.tab.toUpperCase(); color: root.muted; font.pixelSize: Style.font.caption }
            ListView {
              id: entries
              Layout.fillWidth: true
              Layout.fillHeight: true
              clip: true
              model: root.rows
              spacing: Style.space(5)
              boundsBehavior: Flickable.StopAtBounds
              QQC.ScrollBar.vertical: QQC.ScrollBar {}
              delegate: Rectangle {
                id: row
                required property var modelData
                width: entries.width - Style.space(10)
                height: Style.space(90)
                radius: Style.cornerRadius
                readonly property bool chosen: root.selected !== null && (modelData.source ? root.selected.source === modelData.source : root.selected.path === modelData.path)
                color: chosen ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.13) : hover.containsMouse ? root.subtle : "transparent"
                border.width: chosen ? 1 : 0
                border.color: root.accent
                Column {
                  anchors.fill: parent
                  anchors.margins: Style.space(10)
                  spacing: Style.space(6)
                  Label { width: parent.width; text: row.modelData.name + (row.modelData.enabled === false ? " · disabled" : ""); font.bold: true; elide: Text.ElideRight; wrapMode: Text.NoWrap }
                  Label {
                    width: parent.width
                    text: row.modelData.description || row.modelData.source || row.modelData.path || ""
                    color: root.muted; font.pixelSize: Style.font.caption; maximumLineCount: 2; elide: Text.ElideRight
                  }
                }
                MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; enabled: !root.busy; onClicked: root.selectRow(row.modelData) }
              }
              Label {
                anchors.centerIn: parent
                width: parent.width - Style.space(16)
                visible: root.rows.length === 0
                text: root.busy ? "Loading…" : root.tab === "Browse" ? "Search the catalog or paste a package source to review it." : "No matching " + root.tab.toLowerCase() + "."
                horizontalAlignment: Text.AlignHCenter; color: root.muted
              }
            }
            RowLayout {
              visible: root.tab === "Browse" && root.total > root.results.length
              ActionButton { text: "Previous"; enabled: !root.busy && root.offset > 0; onClicked: root.searchPackages(Math.max(0, root.offset - 30)) }
              Item { Layout.fillWidth: true }
              ActionButton { text: "Next"; enabled: !root.busy && root.offset + root.results.length < root.total; onClicked: root.searchPackages(root.offset + root.results.length) }
            }
          }
          Rectangle { Layout.fillHeight: true; width: 1; color: root.subtle }
          ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !root.isLibrary
            spacing: Style.space(10)
            RowLayout {
              visible: root.tab === "Browse"
              Layout.fillWidth: true
              TextField { id: sourceField; Layout.fillWidth: true; placeholderText: "npm:package, GitHub URL, or local path"; enabled: !root.busy; onAccepted: root.reviewSource(text.trim()) }
              ActionButton { text: "Review"; enabled: !root.busy && sourceField.text.trim() !== ""; onClicked: root.reviewSource(sourceField.text.trim()) }
            }
            Label { Layout.fillWidth: true; text: root.review ? root.review.name : root.selected ? root.selected.name : "Make Pi your own"; font.pixelSize: Style.font.title; font.bold: true; maximumLineCount: 2; elide: Text.ElideRight }
            Label {
              Layout.fillWidth: true
              text: root.review ? root.review.source : root.selected ? root.selected.source : "Browse community packages, inspect what they contain, then choose what to add."
              color: root.muted; font.pixelSize: Style.font.caption; maximumLineCount: 2; elide: Text.ElideMiddle
            }
            RowLayout {
              visible: root.tab === "Installed" && root.selected !== null
              ActionButton { text: "Review files"; onClicked: root.send({action: "review", source: root.selected.source}) }
              ActionButton { text: "Review update"; enabled: !root.busy && root.piAvailable; onClicked: root.send({action: "reviewUpdate", source: root.selected.source}) }
              ActionButton { text: root.selected && root.selected.enabled ? "Disable" : "Enable"; onClicked: root.confirm((root.selected.enabled ? "Disable " : "Enable ") + root.selected.name + " in " + root.scope + " settings?", {action: "toggle", source: root.selected.source, enabled: !root.selected.enabled}) }
              ActionButton { id: removePackage; text: "Remove"; onClicked: root.confirm("Remove " + root.selected.name + " from " + root.scope + " settings?", {action: "remove", source: root.selected.source, confirm: true}) }
            }
            RowLayout {
              visible: root.review !== null
              ActionButton { text: "Overview"; selected: root.reviewFile === ""; onClicked: root.reviewFile = "" }
              Dropdown {
                id: fileChoice
                Layout.fillWidth: true
                options: root.review ? (root.review.files || []) : []
                value: root.reviewFile || "Inspect package files…"
                onChanged: function(value) { root.reviewFile = value }
              }
            }
            Rectangle {
              Layout.fillWidth: true
              Layout.fillHeight: true
              radius: Style.cornerRadius
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
              border.color: root.subtle
              QQC.ScrollView {
                id: reviewScroll
                anchors.fill: parent
                anchors.margins: 1
                clip: true
                contentWidth: availableWidth
                QQC.ScrollBar.horizontal.policy: QQC.ScrollBar.AlwaysOff
                QQC.TextArea {
                  width: reviewScroll.availableWidth
                  readOnly: true
                  selectByMouse: true
                  textFormat: TextEdit.PlainText
                  wrapMode: TextEdit.Wrap
                  padding: Style.space(14)
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  background: Item {}
                  text: !root.review ? (root.selected ? "Choose Review files to inspect this package.\n\nEnabled: " + (root.selected.enabled ? "Yes" : "No") + "\n\nResource filters\n" + JSON.stringify(root.selected.filters || {}, null, 2) : "A native home for your Pi capabilities.\n\nBrowse\nFind packages from the npm Pi catalog or review a source directly.\n\nInstalled\nReview, enable, disable, update, and remove packages.\n\nExtensions · Skills · Prompts\nBrowse your files, start from a template, and edit them here.\n\nChanges load in new Pi sessions. In an existing session, use /reload.")
                    : root.reviewFile ? ((root.review.fileContents || {})[root.reviewFile] || "This file is binary, too large, or outside the bounded text preview. Review it in the source repository before installing.")
                    : (root.review.description || "") + "\n\nBEFORE YOU INSTALL\n" + (root.review.warnings || []).join("\n\n") + "\n\nPACKAGE MANIFEST\n" + root.review.manifest + "\n\nREADME\n" + (root.review.readme || "No README was provided.")
                }
              }
            }
            Label { visible: root.review !== null; Layout.fillWidth: true; text: "Install scripts are blocked. Extensions can execute code when Pi loads them. A review is not a sandbox."; color: root.muted; font.pixelSize: Style.font.caption }
            RowLayout {
              visible: root.review !== null
              Layout.fillWidth: true
              ActionButton { id: trustButton; text: root.acceptedReview ? "✓ Reviewed & trusted" : "I reviewed and trust this source"; selected: root.acceptedReview; onClicked: root.acceptedReview = !root.acceptedReview }
              Item { Layout.fillWidth: true }
              ActionButton { id: installButton; text: "Install · " + root.scope; selected: true; enabled: !root.busy && root.scopeReady && root.piAvailable && root.acceptedReview; onClicked: root.confirm("Install " + root.review.source + " into " + root.scopePath + "?", {action: "install", source: root.review.source, token: root.review.token}) }
            }
          }
          ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.isLibrary
            spacing: Style.space(10)
            Label { Layout.fillWidth: true; text: root.editing ? (root.resource ? (root.editable ? "Edit " : "Read-only · ") : "New ") + (root.tab === "Skills" ? "skill" : root.tab === "Prompts" ? "prompt" : "extension") + (root.dirty ? " · Unsaved" : "") : "Your " + root.tab.toLowerCase(); font.pixelSize: Style.font.title; font.bold: true }
            Label {
              Layout.fillWidth: true
              text: root.tab === "Skills" ? "Skills teach Pi a workflow. Each needs a name and description in SKILL.md." : root.tab === "Prompts" ? "Prompts expand from /name. Use $1, $2, and $@ for arguments." : "Extensions add tools, commands, and hooks. Saving code does not run it."
              color: root.muted; font.pixelSize: Style.font.caption
            }
            RowLayout {
              visible: root.tab === "Prompts" && !root.resource
              ActionButton { text: "Code review"; onClicked: root.promptStarter("review") }
              ActionButton { text: "Debugging"; onClicked: root.promptStarter("debug") }
              ActionButton { text: "Documentation"; onClicked: root.promptStarter("docs") }
            }
            RowLayout {
              visible: root.tab === "Extensions" && (!root.resource)
              ActionButton { text: "Command starter"; onClicked: root.newResource("command") }
              ActionButton { text: "Custom tool"; onClicked: root.newResource("tool") }
              ActionButton { text: "Permission hook"; onClicked: root.newResource("guard") }
            }
            RowLayout {
              visible: root.editing
              Layout.fillWidth: true
              TextField { id: nameField; Layout.fillWidth: true; placeholderText: "lowercase-name"; readOnly: root.resource !== null; enabled: !root.busy; Keys.onPressed: function(event) { root.handleKey(event) } }
              ActionButton { text: "Copy command"; visible: root.tab !== "Extensions"; enabled: !root.busy && nameField.text.trim() !== ""; onClicked: root.copy((root.tab === "Skills" ? "/skill:" : "/") + nameField.text.trim()) }
            }
            Label { visible: root.resource !== null; Layout.fillWidth: true; text: root.resource ? root.resource.path : ""; color: root.muted; font.pixelSize: Style.font.caption; elide: Text.ElideMiddle; wrapMode: Text.NoWrap }
            Rectangle {
              Layout.fillWidth: true
              Layout.fillHeight: true
              radius: Style.cornerRadius
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.035)
              border.color: bodyField.activeFocus ? root.accent : root.subtle
              QQC.ScrollView {
                id: bodyScroll
                anchors.fill: parent
                anchors.margins: 1
                clip: true
                contentWidth: availableWidth
                QQC.ScrollBar.horizontal.policy: QQC.ScrollBar.AlwaysOff
                QQC.TextArea {
                  id: bodyField
                  width: bodyScroll.availableWidth
                  readOnly: !root.editable || root.busy
                  placeholderText: "Select a file to read it, or create a new " + (root.tab === "Skills" ? "skill" : root.tab === "Prompts" ? "prompt" : "extension") + "."
                  textFormat: TextEdit.PlainText
                  wrapMode: TextEdit.Wrap
                  selectByMouse: true
                  persistentSelection: true
                  padding: Style.space(12)
                  color: root.foreground
                  placeholderTextColor: root.muted
                  selectionColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.4)
                  selectedTextColor: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  background: Item {}
                  Keys.onPressed: function(event) { root.handleKey(event) }
                }
              }
            }
            RowLayout {
              visible: root.editing
              Layout.fillWidth: true
              ActionButton { text: "Copy text"; onClicked: root.copy(bodyField.text) }
              ActionButton {
                text: root.resource && root.resource.enabled ? "Disable" : "Enable"
                visible: root.resource !== null && root.editable
                enabled: !root.busy && !root.dirty && root.scopeReady
                onClicked: root.confirm((root.resource.enabled ? "Disable " : "Enable ") + root.resource.name + " in " + root.scope + " settings?", {action: "toggleResource", path: root.resource.path, enabled: !root.resource.enabled})
              }
              ActionButton { text: "Delete"; visible: root.resource !== null && root.editable; onClicked: root.confirm((root.dirty ? "Discard unsaved edits and move the saved file to recoverable trash?" : "Move this file to Pi Hub’s recoverable trash?"), {action: "delete", path: root.resource.path, revision: root.resource.revision, confirm: true}) }
              Item { Layout.fillWidth: true }
              ActionButton { text: "Save " + (root.tab === "Skills" ? "skill" : root.tab === "Prompts" ? "prompt" : "extension"); selected: root.dirty; enabled: !root.busy && root.editable && root.dirty && nameField.text.trim() !== "" && bodyField.text.trim() !== ""; onClicked: root.save() }
            }
          }
        }
        Rectangle { Layout.fillWidth: true; height: 1; color: root.subtle }
        RowLayout {
          visible: root.pendingNavigation !== null || root.confirmation !== null
          Layout.fillWidth: true
          Label { Layout.fillWidth: true; text: root.confirmation ? root.confirmation.text : "You have unsaved edits. Save them or discard before switching."; color: root.accent }
          ActionButton { visible: root.pendingNavigation !== null; text: "Save & continue"; enabled: !root.busy && root.editable; onClicked: root.save() }
          ActionButton {
            id: confirmAction
            text: root.confirmation ? "Confirm" : "Discard & continue"
            onClicked: {
              if (root.confirmation) { var payload = root.confirmation.payload; root.confirmation = null; root.send(payload) }
              else { var next = root.pendingNavigation; root.pendingNavigation = null; next() }
            }
          }
          ActionButton { text: "Cancel"; onClicked: { root.pendingNavigation = null; root.confirmation = null } }
        }
        RowLayout {
          Layout.fillWidth: true
          Label { Layout.fillWidth: true; text: root.message || (root.dirty ? "Unsaved draft · Close and reopen to keep editing. Ctrl+S saves." : "Changes apply on Pi’s next start or /reload.  ·  Ctrl+F Search  ·  Ctrl+N New  ·  Ctrl+S Save"); color: root.error ? Color.urgent : root.muted; font.pixelSize: Style.font.caption; maximumLineCount: 3; elide: Text.ElideRight }
          ActionButton { visible: root.undoToken !== ""; text: "Undo delete"; enabled: !root.busy && !root.dirty; onClicked: root.send({action: "restore", token: root.undoToken}) }
          ActionButton { text: "Copy /reload"; onClicked: root.copy("/reload") }
        }
      }
    }
  }
}
