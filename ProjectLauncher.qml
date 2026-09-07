import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  property string home: Quickshell.env("HOME")
  property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  property string helper: pluginDir + "/bin/omarchy-project-launcher"
  property bool opened: false
  property string filterText: ""
  property var projects: []
  property int selectedIndex: 0

  function open(payload) {
    root.opened = true
    root.filterText = ""
    root.selectedIndex = 0
    loadProjects()
  }

  function close() { root.opened = false }

  function loadProjects() {
    listProc.command = ["python", helper, "--json"]
    listProc.running = true
  }

  function filteredProjects() {
    var query = root.filterText.toLowerCase()
    if (!query) return root.projects
    return root.projects.filter(function(project) {
      return String(project.name).toLowerCase().indexOf(query) >= 0
        || String(project.status).toLowerCase().indexOf(query) >= 0
    })
  }

  function launchSelected() {
    var rows = filteredProjects()
    if (rows.length === 0) return
    var project = rows[Math.max(0, Math.min(root.selectedIndex, rows.length - 1))]
    launchProc.command = ["python", helper, "--launch", project.path]
    launchProc.running = true
    root.close()
  }

  Process {
    id: listProc
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.projects = JSON.parse(String(text || "[]")) }
        catch (error) { root.projects = [] }
      }
    }
  }

  Process { id: launchProc }

  PanelWindow {
    id: window
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "u64a-project-launcher"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: root.opened ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Qt.rgba(0, 0, 0, 0.45)

      Rectangle {
        width: Math.min(parent.width - Style.space(48), Style.space(620))
        height: Math.min(parent.height - Style.space(96), Style.space(560))
        anchors.centerIn: parent
        radius: Style.cornerRadius
        color: Color.menu.background
        border.color: Color.menu.border
        border.width: 1

        Column {
          anchors.fill: parent
          anchors.margins: Style.space(20)
          spacing: Style.space(12)

          Text {
            text: "Projects"
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.bold: true
          }

          TextField {
            id: search
            width: parent.width
            placeholderText: "Search projects"
            text: root.filterText
            onTextChanged: { root.filterText = text; root.selectedIndex = 0 }
            Keys.onEscapePressed: root.close()
            Keys.onReturnPressed: root.launchSelected()
            Keys.onDownPressed: root.selectedIndex++
            Keys.onUpPressed: root.selectedIndex--
          }

          ListView {
            id: projectList
            width: parent.width
            height: parent.height - y
            clip: true
            model: root.filteredProjects()
            currentIndex: Math.max(0, Math.min(root.selectedIndex, count - 1))

            delegate: Rectangle {
              required property var modelData
              required property int index
              width: projectList.width
              height: Style.space(64)
              radius: Style.cornerRadius
              color: index === projectList.currentIndex ? Color.menu.selectedBackground : "transparent"

              Column {
                anchors.fill: parent
                anchors.margins: Style.space(10)
                spacing: Style.space(4)
                Text {
                  text: modelData.name
                  color: Color.menu.text
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
                Text {
                  text: modelData.status
                  color: Qt.darker(Color.menu.text, 1.35)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              MouseArea {
                anchors.fill: parent
                onClicked: { root.selectedIndex = index; root.launchSelected() }
              }
            }
          }
        }
      }
    }
  }
}
