import QtQuick
import Quickshell
import Quickshell.Io

QtObject {
    id: bounded

    property bool active: false
    property string output: ""
    property string errors: ""
    property string failure: ""
    property int timeoutMs: 80000
    readonly property int outputLimit: 524289
    readonly property int errorLimit: 8192
    signal finished(int exitCode)

    function helperEnvironment() {
        var environment = {
            "PATH": "/usr/bin",
            "OMARCHY_PROJECT_LAUNCHER_USER_PATH": Quickshell.env("PATH") || ""
        };
        var names = [
            "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
            "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "XDG_DATA_HOME", "XDG_DATA_DIRS",
            "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY", "WAYLAND_DISPLAY", "SSH_AUTH_SOCK", "OMARCHY_PROJECTS_ROOT"
        ];
        for (var index = 0; index < names.length; ++index) {
            var value = Quickshell.env(names[index]);
            if (value)
                environment[names[index]] = value;
        }
        return environment;
    }

    function start(command) {
        if (active)
            return false;
        output = "";
        errors = "";
        failure = "";
        active = true;
        child.command = [command[0], "--supervise"].concat(command.slice(1));
        child.running = true;
        watchdog.restart();
        return true;
    }

    function cancel(reason) {
        if (!active || failure)
            return;
        failure = reason || "Operation cancelled";
        output = "";
        errors = "";
        child.signal(15);
        escalation.restart();
    }

    function receive(data, isError) {
        if (failure)
            return;
        var previous = isError ? errors : output;
        var limit = isError ? errorLimit : outputLimit;
        if (data.length > limit - previous.length) {
            cancel("Helper output exceeded its limit");
            return;
        }
        if (isError)
            errors = previous + data;
        else
            output = previous + data;
    }

    property Process child: Process {
        clearEnvironment: true
        environment: bounded.helperEnvironment()
        // An empty marker emits each read immediately: no unlimited partial
        // line buffer. The Python broker also bounds bytes before decoding.
        stdout: SplitParser {
            splitMarker: ""
            onRead: data => bounded.receive(data, false)
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: data => bounded.receive(data, true)
        }
        onExited: function (exitCode) {
            bounded.watchdog.stop();
            bounded.escalation.stop();
            bounded.active = false;
            bounded.finished(bounded.failure ? 1 : exitCode);
        }
        onRunningChanged: {
            if (!running && bounded.active) {
                Qt.callLater(function () {
                    if (!bounded.active)
                        return;
                    bounded.watchdog.stop();
                    bounded.escalation.stop();
                    bounded.active = false;
                    bounded.failure = bounded.failure || "Could not start project helper";
                    bounded.finished(1);
                });
            }
        }
    }

    property Timer watchdog: Timer {
        interval: bounded.timeoutMs
        onTriggered: bounded.cancel("Project helper timed out")
    }
    property Timer escalation: Timer {
        interval: 9000
        onTriggered: bounded.child.signal(9)
    }

    Component.onDestruction: {
        if (active)
            child.signal(15);
    }
}
