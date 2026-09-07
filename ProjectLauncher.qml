import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
    id: root

    property var shell: null
    property var manifest: null
    property string pluginDir: root.manifest && root.manifest.__sourceDir ? String(root.manifest.__sourceDir) : Qt.resolvedUrl(".").toString().replace("file://", "")
    property string helper: pluginDir.replace(/\/$/, "") + "/bin/omarchy-project-launcher"
    property bool opened: false
    property bool loading: false
    property string errorText: ""
    property string filterText: ""
    property var projects: []
    property int selectedIndex: 0

    function open(payloadJson) {
        root.opened = true;
        root.filterText = "";
        root.selectedIndex = 0;
        loadProjects();
        Qt.callLater(function () {
            search.forceActiveFocus();
        });
    }

    function close() {
        root.opened = false;
    }

    function dismiss() {
        root.opened = false;
        if (root.shell && typeof root.shell.hide === "function")
            root.shell.hide((root.manifest && root.manifest.id) || "io.github.u64a.project-launcher");
    }

    function loadProjects() {
        root.loading = true;
        root.errorText = "";
        listProc.command = [helper, "--json"];
        listProc.running = true;
    }

    function filteredProjects() {
        var query = root.filterText.toLowerCase();
        if (!query)
            return root.projects;
        return root.projects.filter(function (project) {
            return String(project.name).toLowerCase().indexOf(query) >= 0 || String(project.status).toLowerCase().indexOf(query) >= 0;
        });
    }

    function launchSelected() {
        var rows = filteredProjects();
        if (rows.length === 0)
            return;
        var project = rows[Math.max(0, Math.min(root.selectedIndex, rows.length - 1))];
        Quickshell.execDetached([helper, "--launch", project.path]);
        root.dismiss();
    }

    Process {
        id: listProc
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    root.projects = JSON.parse(String(text || "[]"));
                } catch (error) {
                    root.projects = [];
                    root.errorText = "Could not read project status";
                }
            }
        }
        stderr: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var detail = String(text || "").trim();
                if (detail)
                    root.errorText = detail;
            }
        }
        onExited: function (exitCode) {
            root.loading = false;
            if (exitCode !== 0 && !root.errorText)
                root.errorText = "Could not scan the projects folder";
        }
    }

    PanelWindow {
        id: window
        visible: root.opened
        anchors {
            top: true
            bottom: true
            left: true
            right: true
        }
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
                        onTextChanged: {
                            root.filterText = text;
                            root.selectedIndex = 0;
                        }
                        Keys.onEscapePressed: root.dismiss()
                        Keys.onReturnPressed: root.launchSelected()
                        Keys.onDownPressed: {
                            var count = root.filteredProjects().length;
                            if (count > 0)
                                root.selectedIndex = (root.selectedIndex + 1) % count;
                        }
                        Keys.onUpPressed: {
                            var count = root.filteredProjects().length;
                            if (count > 0)
                                root.selectedIndex = (root.selectedIndex - 1 + count) % count;
                        }
                    }

                    Text {
                        width: parent.width
                        visible: root.loading || root.errorText || (!root.loading && root.projects.length === 0)
                        text: root.loading ? "Scanning projects…" : root.errorText ? root.errorText : "No Git repositories found directly under ~/Projects"
                        color: root.errorText ? "#ef4444" : Color.menu.text
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        wrapMode: Text.WordWrap
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
                                onClicked: {
                                    root.selectedIndex = index;
                                    root.launchSelected();
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
