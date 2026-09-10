import QtQuick
import QtQuick.Controls
import Quickshell
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
    property bool operating: false
    property string errorText: ""
    property string operationError: ""
    property string filterText: ""
    property string viewMode: "list"
    property string addKind: "clone"
    property string cloneUrl: ""
    property string projectName: ""
    property string importPath: ""
    property string importMethod: "symlink"
    property var deleteProject: null
    property int deleteStep: 1
    property var projects: []
    property int selectedIndex: 0
    property bool settingsLoading: false
    property var launcherOptions: []
    property string launcherChoice: "copilot"
    property string customCommand: ""
    property bool launching: false

    function open(payloadJson) {
        root.opened = true;
        root.filterText = "";
        root.viewMode = "list";
        root.operationError = "";
        root.selectedIndex = 0;
        loadProjects();
        Qt.callLater(function () {
            search.forceActiveFocus();
        });
    }

    function close() {
        cancelWork();
        root.opened = false;
    }

    function dismiss() {
        cancelWork();
        root.opened = false;
        if (root.shell && typeof root.shell.hide === "function")
            root.shell.hide((root.manifest && root.manifest.id) || "io.github.u64a.project-launcher");
    }

    function loadProjects() {
        if (listProc.active)
            return;
        root.loading = true;
        root.errorText = "";
        root.projects = [];
        listProc.start([helper, "--json"]);
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
        if (root.operating)
            return;
        var rows = filteredProjects();
        if (rows.length === 0)
            return;
        var project = rows[Math.max(0, Math.min(root.selectedIndex, rows.length - 1))];
        root.launching = true;
        runOperation([helper, "--launch", project.path]);
    }

    function showSetup() {
        if (settingsProc.active)
            return;
        root.operationError = "";
        root.viewMode = "setup";
        root.settingsLoading = true;
        root.launcherOptions = [];
        settingsProc.start([helper, "--settings"]);
    }

    function saveSetup() {
        var command = [helper, "--set-launcher", root.launcherChoice];
        if (root.launcherChoice === "custom")
            command.push("--custom-command", root.customCommand);
        runOperation(command);
    }

    function showAdd(kind) {
        root.addKind = kind || "clone";
        root.cloneUrl = "";
        root.projectName = "";
        root.importPath = "";
        root.importMethod = "symlink";
        root.operationError = "";
        root.viewMode = "add";
        Qt.callLater(function () {
            addInput.forceActiveFocus();
        });
    }

    function runAdd() {
        var command = [helper];
        if (root.addKind === "clone")
            command.push("--clone", root.cloneUrl);
        else if (root.addKind === "create")
            command.push("--create", root.projectName);
        else
            command.push("--import", root.importPath, "--import-method", root.importMethod);
        runOperation(command);
    }

    function requestDelete(project) {
        root.deleteProject = project;
        root.deleteStep = 1;
        root.operationError = "";
        root.viewMode = "delete";
        Qt.callLater(function () {
            deleteActionButton.forceActiveFocus();
        });
    }

    function projectNeedsConfirmation(project) {
        return project.changed > 0 || project.untracked > 0 || String(project.error || "") !== "";
    }

    function confirmDelete() {
        if (!root.deleteProject)
            return;
        var dirty = root.projectNeedsConfirmation(root.deleteProject);
        if (dirty && root.deleteStep === 1) {
            root.deleteStep = 2;
            return;
        }
        var command = [helper, "--trash", root.deleteProject.path];
        if (dirty)
            command.push("--allow-dirty");
        runOperation(command);
    }

    function runOperation(command) {
        if (operationProc.active)
            return;
        root.operating = true;
        root.operationError = "";
        operationProc.start(command);
    }

    function cancelWork() {
        listProc.cancel("Operation cancelled");
        settingsProc.cancel("Operation cancelled");
        operationProc.cancel("Operation cancelled");
    }

    function returnToList() {
        if (root.operating || root.settingsLoading) {
            root.cancelWork();
            return;
        }
        root.viewMode = "list";
        root.operationError = "";
        loadProjects();
        Qt.callLater(function () {
            search.forceActiveFocus();
        });
    }

    Shortcut {
        sequence: "Escape"
        enabled: root.opened
        onActivated: {
            if (root.operating || root.settingsLoading || root.loading)
                root.cancelWork();
            else if (root.viewMode === "list")
                root.dismiss();
            else
                root.returnToList();
        }
    }

    BoundedProcess {
        id: listProc
        onFinished: function (exitCode) {
            root.loading = false;
            root.errorText = failure || errors.trim();
            if (exitCode === 0 && !root.errorText) {
                try {
                    var rows = JSON.parse(output);
                    if (!Array.isArray(rows) || rows.length > 256)
                        throw new Error("Project count limit");
                    root.projects = rows;
                } catch (error) {
                    root.projects = [];
                    root.errorText = "Could not read project status";
                }
            } else {
                root.projects = [];
                root.errorText = root.errorText || "Could not scan the projects folder";
            }
        }
    }

    BoundedProcess {
        id: settingsProc
        timeoutMs: 50000
        onFinished: function (exitCode) {
            root.settingsLoading = false;
            root.operationError = failure || errors.trim();
            if (exitCode === 0 && !root.operationError) {
                try {
                    var settings = JSON.parse(output);
                    if (!Array.isArray(settings.options) || settings.options.length > 5)
                        throw new Error("Launcher count limit");
                    root.launcherOptions = settings.options;
                    root.launcherChoice = settings.launcher;
                    root.customCommand = settings.custom_command;
                    root.operationError = settings.error;
                } catch (error) {
                    root.operationError = "Could not read launcher settings";
                }
            } else
                root.operationError = root.operationError || "Could not load launcher settings";
            Qt.callLater(function () {
                if (root.viewMode !== "setup")
                    return;
                for (var i = 0; i < launcherRepeater.count; i++) {
                    var option = launcherRepeater.itemAt(i);
                    if (option && option.enabled) {
                        option.forceActiveFocus();
                        return;
                    }
                }
                setupCancel.forceActiveFocus();
            });
        }
    }

    BoundedProcess {
        id: operationProc
        timeoutMs: 320000
        onFinished: function (exitCode) {
            root.operating = false;
            root.operationError = failure || errors.trim();
            var wasLaunching = root.launching;
            root.launching = false;
            if (exitCode === 0) {
                if (wasLaunching)
                    root.dismiss();
                else if (root.opened)
                    root.returnToList();
            }
            else if (!root.operationError)
                root.operationError = wasLaunching ? "Could not open the launcher" : "Project operation failed";
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

                    Row {
                        width: parent.width
                        spacing: Style.space(10)

                        Text {
                            width: root.viewMode === "list" ? parent.width - addButton.width - setupButton.width - parent.spacing * 2 : parent.width
                            anchors.verticalCenter: parent.verticalCenter
                            text: root.viewMode === "list" ? "Projects" : root.viewMode === "setup" ? "Launcher setup" : root.viewMode === "add" ? "Add project" : "Move project to Trash"
                            color: Color.menu.text
                            font.family: Style.font.family
                            font.pixelSize: Style.font.title
                            font.bold: true
                        }

                        Button {
                            id: addButton
                            visible: root.viewMode === "list"
                            focusable: true
                            enabled: !root.operating
                            text: "+ Add"
                            onClicked: root.showAdd("clone")
                        }

                        Button {
                            id: setupButton
                            visible: root.viewMode === "list"
                            focusable: true
                            enabled: !root.operating
                            text: "Setup"
                            onClicked: root.showSetup()
                        }
                    }

                    TextField {
                        id: search
                        maximumLength: 256
                        enabled: !root.operating
                        visible: root.viewMode === "list"
                        activeFocusOnTab: true
                        width: parent.width
                        placeholderText: "Search projects"
                        text: root.filterText
                        onTextChanged: {
                            root.filterText = text;
                            root.selectedIndex = 0;
                        }
                        Keys.onEscapePressed: root.dismiss()
                        Keys.onReturnPressed: root.launchSelected()
                        Keys.onDeletePressed: {
                            var rows = root.filteredProjects();
                            if (rows.length > 0)
                                root.requestDelete(rows[Math.max(0, Math.min(root.selectedIndex, rows.length - 1))]);
                        }
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
                        visible: root.viewMode === "list" && root.operationError !== ""
                        width: parent.width
                        text: root.operationError
                        color: "#ef4444"
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        wrapMode: Text.WordWrap
                    }

                    ScrollView {
                        id: setupScroll
                        visible: root.viewMode === "setup"
                        width: parent.width
                        height: parent.height - y
                        contentWidth: availableWidth
                        clip: true

                        Column {
                            width: setupScroll.availableWidth
                            spacing: Style.space(8)

                            Text {
                                width: parent.width
                                text: root.settingsLoading ? "Loading launchers..." : "Choose what opens in a project's working directory."
                                color: Color.menu.text
                                font.family: Style.font.family
                                font.pixelSize: Style.font.body
                                wrapMode: Text.WordWrap
                            }

                            FocusScope {
                                id: setupChoices
                                width: parent.width
                                height: choicesColumn.implicitHeight

                                Column {
                                    id: choicesColumn
                                    width: parent.width
                                    spacing: Style.space(4)

                                    Repeater {
                                        id: launcherRepeater
                                        model: root.launcherOptions
                                        Button {
                                            required property var modelData
                                            required property int index
                                            width: choicesColumn.width
                                            focusable: true
                                            focus: index === 0
                                            enabled: !root.operating && modelData.available
                                            selected: root.launcherChoice === modelData.id
                                            leftAlign: true
                                            opacity: enabled ? 1 : 0.5
                                            text: (root.launcherChoice === modelData.id ? "● " : "") + modelData.name + (modelData.available ? "" : " (not installed)")
                                            onClicked: root.launcherChoice = modelData.id
                                        }
                                    }
                                }
                            }

                            TextField {
                                visible: root.launcherChoice === "custom"
                                width: parent.width
                                enabled: !root.operating
                                activeFocusOnTab: true
                                placeholderText: "my-launcher --option \"quoted argument\""
                                text: root.customCommand
                                onTextChanged: root.customCommand = text
                            }

                            Text {
                                visible: root.launcherChoice === "custom"
                                width: parent.width
                                text: "Enter an executable and arguments. Quoting is supported; shell operators and variable expansion are not. Use an absolute path for executables outside PATH."
                                color: Color.menu.text
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                wrapMode: Text.WordWrap
                            }

                            Text {
                                visible: root.operationError !== ""
                                width: parent.width
                                text: root.operationError
                                color: "#ef4444"
                                font.family: Style.font.family
                                font.pixelSize: Style.font.body
                                wrapMode: Text.WordWrap
                            }

                            Row {
                                spacing: Style.space(8)
                                Button {
                                    focusable: true
                                    text: root.operating ? "Saving..." : "Save"
                                    enabled: !root.operating && !root.settingsLoading && root.launcherChoice !== "" && (root.launcherChoice !== "custom" || root.customCommand.trim() !== "")
                                    onClicked: root.saveSetup()
                                }
                                Button {
                                    id: setupCancel
                                    focusable: true
                                    text: "Cancel"
                                    onClicked: root.returnToList()
                                }
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: root.viewMode === "list" && (root.loading || root.errorText || (!root.loading && root.projects.length === 0))
                        text: root.loading ? "Scanning projects…" : root.errorText ? root.errorText : "No Git repositories found directly under ~/Projects"
                        color: root.errorText ? "#ef4444" : Color.menu.text
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        wrapMode: Text.WordWrap
                    }

                    Column {
                        visible: root.viewMode === "add"
                        width: parent.width
                        spacing: Style.space(12)

                        Row {
                            spacing: Style.space(8)
                            Button {
                                activeFocusOnTab: true
                                text: (root.addKind === "clone" ? "● " : "") + "Clone URL"
                                onClicked: root.showAdd("clone")
                            }
                            Button {
                                activeFocusOnTab: true
                                text: (root.addKind === "create" ? "● " : "") + "Create new"
                                onClicked: root.showAdd("create")
                            }
                            Button {
                                activeFocusOnTab: true
                                text: (root.addKind === "import" ? "● " : "") + "Import folder"
                                onClicked: root.showAdd("import")
                            }
                        }

                        Text {
                            width: parent.width
                            text: root.addKind === "clone" ? "Clone an HTTPS or SSH Git repository into ~/Projects." : root.addKind === "create" ? "Create a folder and initialize an empty Git repository on main." : "Enter an existing Git repository folder and choose how to import it."
                            color: Color.menu.text
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            wrapMode: Text.WordWrap
                        }

                        TextField {
                            id: addInput
                            activeFocusOnTab: true
                            width: parent.width
                            placeholderText: root.addKind === "clone" ? "https://github.com/owner/repository.git" : root.addKind === "create" ? "Project name" : "/home/user/path/to/repository"
                            text: root.addKind === "clone" ? root.cloneUrl : root.addKind === "create" ? root.projectName : root.importPath
                            onTextChanged: {
                                if (root.addKind === "clone")
                                    root.cloneUrl = text;
                                else if (root.addKind === "create")
                                    root.projectName = text;
                                else
                                    root.importPath = text;
                            }
                            Keys.onEscapePressed: root.returnToList()
                            Keys.onReturnPressed: root.runAdd()
                        }

                        Column {
                            id: importMethodGroup
                            visible: root.addKind === "import"
                            width: parent.width
                            spacing: Style.space(6)

                            Text {
                                text: "Import as"
                                color: Qt.darker(Color.menu.text, 1.35)
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                            }

                            Repeater {
                                model: [
                                    {
                                        value: "symlink",
                                        label: "Symlink (recommended)",
                                        hint: "Link to the folder in place; the original is never moved."
                                    },
                                    {
                                        value: "move",
                                        label: "Move",
                                        hint: "Relocate the repository into the projects folder."
                                    },
                                    {
                                        value: "copy",
                                        label: "Copy",
                                        hint: "Duplicate the repository, leaving the original untouched."
                                    }
                                ]

                                Rectangle {
                                    id: methodRow
                                    property bool selected: root.importMethod === modelData.value
                                    width: importMethodGroup.width
                                    height: methodText.implicitHeight + Style.space(20)
                                    radius: Style.cornerRadius
                                    color: methodRow.selected ? Color.menu.selectedBackground : "transparent"
                                    border.color: methodRow.activeFocus ? Color.menu.text : methodRow.selected ? Color.menu.border : "transparent"
                                    border.width: 1
                                    activeFocusOnTab: root.addKind === "import"

                                    Keys.onSpacePressed: root.importMethod = modelData.value
                                    Keys.onReturnPressed: root.importMethod = modelData.value
                                    Keys.onEscapePressed: root.returnToList()

                                    Rectangle {
                                        id: radioMark
                                        anchors.left: parent.left
                                        anchors.leftMargin: Style.space(10)
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: Style.space(16)
                                        height: width
                                        radius: width / 2
                                        color: "transparent"
                                        border.color: methodRow.selected ? Color.menu.text : Qt.darker(Color.menu.text, 1.6)
                                        border.width: 1

                                        Rectangle {
                                            anchors.centerIn: parent
                                            width: parent.width - Style.space(8)
                                            height: width
                                            radius: width / 2
                                            visible: methodRow.selected
                                            color: Color.menu.text
                                        }
                                    }

                                    Column {
                                        id: methodText
                                        anchors.left: radioMark.right
                                        anchors.leftMargin: Style.space(10)
                                        anchors.right: parent.right
                                        anchors.rightMargin: Style.space(10)
                                        anchors.verticalCenter: parent.verticalCenter
                                        spacing: Style.space(2)

                                        Text {
                                            width: parent.width
                                            text: modelData.label
                                            color: Color.menu.text
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.body
                                            font.bold: methodRow.selected
                                            elide: Text.ElideRight
                                        }

                                        Text {
                                            width: parent.width
                                            text: modelData.hint
                                            color: Qt.darker(Color.menu.text, 1.35)
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.caption
                                            wrapMode: Text.WordWrap
                                        }
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            root.importMethod = modelData.value;
                                            methodRow.forceActiveFocus();
                                        }
                                    }
                                }
                            }
                        }

                        Text {
                            visible: root.operationError !== ""
                            width: parent.width
                            text: root.operationError
                            color: "#ef4444"
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            wrapMode: Text.WordWrap
                        }

                        Row {
                            spacing: Style.space(8)
                            Button {
                                activeFocusOnTab: true
                                text: root.operating ? "Working…" : root.addKind === "clone" ? "Clone" : root.addKind === "create" ? "Create" : "Import"
                                enabled: !root.operating && addInput.text.trim() !== ""
                                onClicked: root.runAdd()
                            }
                            Button {
                                activeFocusOnTab: true
                                text: "Cancel"
                                onClicked: root.returnToList()
                            }
                        }
                    }

                    Column {
                        visible: root.viewMode === "delete" && root.deleteProject !== null
                        width: parent.width
                        spacing: Style.space(12)

                        Text {
                            width: parent.width
                            text: root.deleteStep === 2 ? (root.deleteProject && String(root.deleteProject.error || "") !== "" ? "This repository's status could not be verified. Move it to Trash anyway?" : "This repository has uncommitted changes. Move it to Trash anyway?") : "Move ‘" + (root.deleteProject ? root.deleteProject.name : "") + "’ to the desktop Trash?"
                            color: root.deleteStep === 2 ? "#ef4444" : Color.menu.text
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            font.bold: root.deleteStep === 2
                            wrapMode: Text.WordWrap
                        }

                        Text {
                            width: parent.width
                            text: root.deleteProject ? root.deleteProject.status : ""
                            color: Color.menu.text
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                            wrapMode: Text.WordWrap
                        }

                        Text {
                            visible: root.operationError !== ""
                            width: parent.width
                            text: root.operationError
                            color: "#ef4444"
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            wrapMode: Text.WordWrap
                        }

                        Row {
                            spacing: Style.space(8)
                            Button {
                                id: deleteActionButton
                                activeFocusOnTab: true
                                text: root.operating ? "Moving…" : root.deleteStep === 2 ? "Move dirty project to Trash" : "Move to Trash"
                                enabled: !root.operating
                                onClicked: root.confirmDelete()
                            }
                            Button {
                                activeFocusOnTab: true
                                text: "Cancel"
                                onClicked: root.returnToList()
                            }
                        }
                    }

                    ListView {
                        id: projectList
                        enabled: !root.operating
                        visible: root.viewMode === "list"
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
                                anchors.rightMargin: trashButton.width + Style.space(20)
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

                            Button {
                                id: trashButton
                                z: 2
                                activeFocusOnTab: true
                                anchors.right: parent.right
                                anchors.rightMargin: Style.space(10)
                                anchors.verticalCenter: parent.verticalCenter
                                text: "Trash"
                                onClicked: root.requestDelete(modelData)
                            }
                        }
                    }
                }
            }
        }
    }
}
