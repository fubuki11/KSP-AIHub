using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using KSPAIHub.API;
using UnityEngine;

namespace KSPAIHub
{
    [Serializable] internal sealed class LaunchSettings { public string pythonExecutable = ""; public string configFile = ""; public bool autoStart = true; }
    [KSPAddon(KSPAddon.Startup.MainMenu, true)]
    public sealed class HubAddon : MonoBehaviour
    {
        private const string Lock = "KSPAIHub.Window";
        private string root, connectionPath, settingsPath;
        private LaunchSettings settings = new LaunchSettings();
        private Process ownedProcess;
        private Task<string> pending;
        private CancellationTokenSource cancellation = new CancellationTokenSource();
        private string operation, status = "Not connected", clientId = "", model = "", apiKey = "", processMessage;
        private AiHubClient client;
        private HubUi ui;
        private bool shown, showSettings, triedRefresh, destroyed, overrideClient, showClients;
        private string[] knownClients = new string[0];
        private Vector2 clientScroll;
        private Vector2 bodyScroll, profileScroll, modelScroll;
        private bool showAddProfile, discoverCustom = true;
        private int presetIndex = 2, protocolIndex, zenProtocolIndex;
        private string newProfileId = "deepseek-api", customBaseUrl = "https://your-provider.example/v1";
        private string modelsForProfile = "", modelFilter = "", modelsStatus = "", modelsMessage = "";
        private string[] modelIds = new string[0], modelLabels = new string[0];
        private static readonly string[] Protocols = { "chat_completions", "responses", "messages" };
        private static readonly string[] ZenProtocols = { "Auto (Zen)", "chat_completions", "responses", "messages" };
        private static readonly string[] Efforts = { "provider_default", "none", "minimal", "low", "medium", "high", "xhigh", "max" };
        private static readonly string[] Thinking = { "provider_default", "enabled", "disabled" };
        private static readonly string[] Repetition = { "disabled", "prompt_only", "disable_thinking", "low_effort" };
        private bool showGeneration, streamGeneration;
        private int effortIndex, thinkingIndex, repetitionIndex;
        private string outputTokens = "16000", recoveryTokens = "32000", generationTimeout = "300";
        private Rect window = new Rect(550, 70, 600, 500);

        private void Start()
        {
            DontDestroyOnLoad(gameObject);
            root = Path.Combine(KSPUtil.ApplicationRootPath, "GameData", "KSPAIHub");
            settingsPath = Path.Combine(root, "PluginData", "launcher.json");
            connectionPath = Path.Combine(root, "PluginData", "connection.json");
            try
            {
                if (File.Exists(settingsPath)) JsonUtility.FromJsonOverwrite(File.ReadAllText(settingsPath), settings);
                if (settings.autoStart) StartService();
            }
            catch (Exception error) { status = error.Message; }
        }
        private void StartService()
        {
            try
            {
                if (ownedProcess != null && !ownedProcess.HasExited) { status = "Service already started by this manager."; return; }
                if (!Path.IsPathRooted(settings.pythonExecutable) || !File.Exists(settings.pythonExecutable) || !File.Exists(settings.configFile))
                    throw new InvalidOperationException("Set the Python executable and Hub configuration paths.");
                var arguments = "-m ksp_aihub --config " + Arg(settings.configFile) + " --state " + Arg(Path.Combine(root, "PluginData", "Private")) +
                    " serve --connection-file " + Arg(connectionPath);
                var info = new ProcessStartInfo(settings.pythonExecutable, arguments) { WorkingDirectory = root, UseShellExecute = false,
                    CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
                info.EnvironmentVariables["PYTHONPATH"] = Path.Combine(root, "Service");
                info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                ownedProcess = new Process { StartInfo = info };
                ownedProcess.OutputDataReceived += Capture; ownedProcess.ErrorDataReceived += Capture;
                ownedProcess.Start(); ownedProcess.BeginOutputReadLine(); ownedProcess.BeginErrorReadLine();
                status = "Starting AI Hub…"; triedRefresh = false;
            }
            catch (Exception error) { status = error.Message; }
        }
        private void Capture(object sender, DataReceivedEventArgs args)
        {
            if (!string.IsNullOrEmpty(args.Data)) processMessage = args.Data.Length > 800 ? args.Data.Substring(0, 800) : args.Data;
        }
        private void Refresh()
        {
            try
            {
                if (overrideClient && string.IsNullOrWhiteSpace(clientId)) throw new InvalidOperationException("Choose a consumer only when configuring an individual override.");
                client = AiHubClient.FromConnectionFile(connectionPath, overrideClient ? clientId : null);
                Begin("ui", client.GetUiAsync(cancellation.Token));
            }
            catch (Exception error) { status = error.Message; }
        }
        private void Begin(string name, Task<string> task) { operation = name; pending = task; status = "Working…"; }
        private void FetchModels(bool force)
        {
            if (client == null || ui == null || !ui.routingReady || pending != null) return;
            modelsForProfile = ui.selectedProfile;
            modelsStatus = "loading"; modelsMessage = "Fetching current model IDs…";
            Begin("models", client.GetModelsAsync(ui.selectedProfile, force, cancellation.Token));
        }
        private void Update()
        {
            if (destroyed) return;
            if (processMessage != null)
            {
                status = processMessage; processMessage = null;
                if (status.StartsWith("KSP AI Hub listening at ", StringComparison.Ordinal)) triedRefresh = false;
            }
            if (!triedRefresh && pending == null && File.Exists(connectionPath)) { triedRefresh = true; Refresh(); }
            if (pending == null || !pending.IsCompleted) return;
            var completed = pending; string finishedOperation = operation; pending = null;
            try
            {
                if (completed.IsFaulted) throw completed.Exception.GetBaseException();
                if (completed.IsCanceled) throw new OperationCanceledException();
                var result = JsonUtility.FromJson<HubUi>(completed.Result);
                if (result == null || !result.ok) throw new InvalidOperationException(result == null ? "Invalid Hub reply." : result.message);
                if (finishedOperation == "login")
                {
                    Uri url;
                    if (!Uri.TryCreate(result.authorizationUrl, UriKind.Absolute, out url) || (url.Scheme != "https" && !(url.Scheme == "http" && url.IsLoopback)))
                        throw new InvalidOperationException("Invalid authorization URL.");
                    Application.OpenURL(result.authorizationUrl); status = "Complete browser sign-in, then Refresh.";
                }
                else if (finishedOperation == "key") { apiKey = ""; modelsForProfile = ""; Refresh(); }
                else if (finishedOperation == "generation") Refresh();
                else if (finishedOperation == "add")
                {
                    showAddProfile = false; apiKey = "";
                    Begin("ui", client.SelectAsync(result.profile, null, cancellation.Token));
                }
                else if (finishedOperation == "models")
                {
                    if (ui != null && result.profile == ui.selectedProfile)
                    {
                        modelIds = result.modelIds ?? new string[0]; modelLabels = result.modelLabels ?? new string[0];
                        modelsStatus = result.modelsStatus; modelsMessage = result.modelsMessage;
                        status = modelsStatus == "ready" ? modelIds.Length + " models " + (result.cached ? "(cached)." : "fetched from provider.") : modelsMessage;
                        if (result.fetchedAt > 0) modelsMessage = "Fetched " + DateTimeOffset.FromUnixTimeSeconds(result.fetchedAt).UtcDateTime.ToString("u") +
                            (result.cached ? " (5-minute cache). " : ". ") + modelsMessage;
                    }
                }
                else
                {
                    ui = result; model = result.model; knownClients = result.clientIds ?? new string[0];
                    outputTokens = result.maxOutputTokens.ToString(); recoveryTokens = result.recoveryMaxOutputTokens.ToString();
                    generationTimeout = Math.Ceiling(result.timeoutSeconds).ToString(System.Globalization.CultureInfo.InvariantCulture);
                    streamGeneration = result.streamEnabled;
                    effortIndex = Math.Max(0, Array.IndexOf(Efforts, result.reasoningEffort));
                    thinkingIndex = Math.Max(0, Array.IndexOf(Thinking, result.thinkingMode));
                    repetitionIndex = Math.Max(0, Array.IndexOf(Repetition, result.repetitionRecovery));
                    status = result.routingReady ? (result.inheritsGlobal ? "Inheriting global default. " : "") + "Auth: " + result.authState : result.routeError;
                    if (modelsForProfile != result.selectedProfile)
                    {
                        modelIds = new string[0]; modelLabels = new string[0]; modelFilter = "";
                        modelsStatus = ""; modelsMessage = "Save an API key to fetch model IDs automatically, or enter an ID manually.";
                        if (result.routingReady && (result.authState == "ready" || result.authState == "refresh_needed")) FetchModels(false);
                    }
                }
            }
            catch (Exception error)
            {
                status = error.Message;
                if (finishedOperation == "models") { modelsStatus = "error"; modelsMessage = status; }
            }
        }
        private void OnGUI()
        {
            if (GUI.Button(new Rect(Screen.width - 135, 35, 125, 28), "AI Hub")) { shown = !shown; if (!shown) { apiKey = ""; GUI.FocusControl(null); } }
            if (!shown) { InputLockManager.RemoveControlLock(Lock); return; }
            window = GUILayout.Window(GetInstanceID(), window, Draw, "KSP AI Hub 0.4.0 — fubuki11st", GUILayout.Width(600));
            bool inside = window.Contains(Event.current.mousePosition);
            if (!inside && Event.current.type == EventType.MouseDown) GUI.FocusControl(null);
            if (inside || (GUI.GetNameOfFocusedControl() ?? "").StartsWith("KSPAIHub."))
                InputLockManager.SetControlLock(ControlTypes.EDITOR_LOCK | ControlTypes.ALL_SHIP_CONTROLS, Lock);
            else InputLockManager.RemoveControlLock(Lock);
        }
        private void Draw(int id)
        {
            GUILayout.Label(status);
            bodyScroll = GUILayout.BeginScrollView(bodyScroll, GUILayout.Height(Mathf.Clamp(Screen.height - 220, 250, 680)));
            GUI.enabled = pending == null;
            bool nextOverride = GUILayout.Toggle(overrideClient, "Configure an individual Mod override (optional)");
            if (nextOverride != overrideClient)
            {
                overrideClient = nextOverride; client = null; ui = null; apiKey = "";
                if (!overrideClient || !string.IsNullOrWhiteSpace(clientId)) Refresh();
            }
            if (!overrideClient) GUILayout.Label("Global default: shared by every consumer without an override.");
            else
            {
                GUILayout.Label("Consumer ID (supplied by that Mod; not an OAuth client ID)"); GUI.SetNextControlName("KSPAIHub.client");
                string nextClient = GUILayout.TextField(clientId, 80);
                if (nextClient != clientId) { clientId = nextClient; client = null; ui = null; apiKey = ""; status = "Refresh to load this consumer's effective route."; }
                if (GUILayout.Button(showClients ? "Hide known consumers" : "Choose a known consumer")) showClients = !showClients;
                if (showClients)
                {
                    clientScroll = GUILayout.BeginScrollView(clientScroll, GUILayout.Height(100));
                    foreach (var idValue in knownClients)
                    {
                        if (GUILayout.Button(idValue)) { clientId = idValue; showClients = false; apiKey = ""; Refresh(); }
                    }
                    if (knownClients.Length == 0) GUILayout.Label("Consumers appear here after calling the Hub, or can be entered manually.");
                    GUILayout.EndScrollView();
                }
            }
            GUILayout.BeginHorizontal();
            if (GUILayout.Button("Refresh")) Refresh();
            if (GUILayout.Button("Start service")) StartService();
            if (GUILayout.Button("Stop owned service"))
            {
                try { if (ownedProcess != null && !ownedProcess.HasExited) ownedProcess.Kill(); status = "Owned service stopped."; }
                catch (Exception error) { status = error.Message; }
            }
            if (GUILayout.Button("Settings")) showSettings = !showSettings;
            GUILayout.EndHorizontal();
            if (showSettings)
            {
                GUILayout.Label("Python executable"); GUI.SetNextControlName("KSPAIHub.python"); settings.pythonExecutable = GUILayout.TextField(settings.pythonExecutable, 2000);
                GUILayout.Label("Hub JSON configuration"); GUI.SetNextControlName("KSPAIHub.config"); settings.configFile = GUILayout.TextField(settings.configFile, 2000);
                settings.autoStart = GUILayout.Toggle(settings.autoStart, "Auto-start owned service at the main menu");
                if (GUILayout.Button("Save launcher settings"))
                {
                    try { Directory.CreateDirectory(Path.GetDirectoryName(settingsPath)); File.WriteAllText(settingsPath, JsonUtility.ToJson(settings, true), new UTF8Encoding(false)); status = "Launcher settings saved."; }
                    catch (Exception error) { status = error.Message; }
                }
            }
            if (ui != null && client != null)
            {
                GUILayout.Label(overrideClient ? "Set an override for this consumer's future requests" : "Select the global default profile");
                profileScroll = GUILayout.BeginScrollView(profileScroll, GUILayout.Height(110));
                for (int i = 0; i < ui.profileIds.Length; i++)
                {
                    if (GUILayout.Button(ui.profileLabels[i])) { apiKey = ""; Begin("ui", client.SelectAsync(ui.profileIds[i], null, cancellation.Token)); }
                }
                GUILayout.EndScrollView();
                if (GUILayout.Button(showAddProfile ? "Hide connection setup" : "Add provider / compatible API")) showAddProfile = !showAddProfile;
                if (showAddProfile) DrawAddProfile();
                GUILayout.Label("Selected: " + ui.selectedProfile + " | " + ui.protocol);
                GUILayout.Label(ui.baseUrl);
                if (GUILayout.Button(showGeneration ? "Hide generation limits" : "Generation limits / reasoning")) showGeneration = !showGeneration;
                if (showGeneration && ui.routingReady) DrawGeneration();
                GUILayout.Label("Authentication: " + ui.authKind + " / " + ui.authState);
                if (ui.authKind == "oauth_managed" && GUILayout.Button("Sign in with browser (PKCE)")) Begin("login", client.BeginLoginAsync(ui.credential, cancellation.Token));
                if (ui.authKind == "api_key_store")
                {
                    GUILayout.Label("API key (saved using Windows encryption)");
                    GUI.SetNextControlName("KSPAIHub.key"); apiKey = GUILayout.PasswordField(apiKey, '*', 20000);
                    if (GUILayout.Button("Store API key and fetch models")) { Begin("key", client.StoreApiKeyAsync(ui.credential, apiKey, cancellation.Token)); apiKey = ""; }
                }
                DrawModels();
                GUILayout.Label("Model ID (list selection or manual entry)"); GUI.SetNextControlName("KSPAIHub.model"); model = GUILayout.TextField(model, 200);
                GUI.enabled = pending == null && ui.routingReady;
                if (GUILayout.Button(overrideClient ? "Apply consumer override" : "Apply global model")) Begin("ui", client.SelectAsync(ui.selectedProfile, model, cancellation.Token));
                GUI.enabled = pending == null;
                if (GUILayout.Button(overrideClient ? "Use global default (clear this override)" : "Reset global default to configuration"))
                    Begin("ui", client.InheritDefaultsAsync(cancellation.Token));
            }
            GUILayout.EndScrollView();
            GUI.enabled = true;
            GUI.DragWindow();
        }
        private void DrawAddProfile()
        {
            var ids = ui.presetIds ?? new string[0]; var labels = ui.presetLabels ?? new string[0];
            if (ids.Length == 0 || labels.Length != ids.Length) return;
            presetIndex = Mathf.Clamp(presetIndex, 0, ids.Length - 1);
            int next = GUILayout.SelectionGrid(presetIndex, labels, 3);
            if (next != presetIndex) { presetIndex = next; newProfileId = ids[next] + "-api"; }
            string preset = ids[presetIndex];
            GUILayout.Label("New profile ID (unique)"); GUI.SetNextControlName("KSPAIHub.newId"); newProfileId = GUILayout.TextField(newProfileId, 64);
            if (preset == "custom")
            {
                GUILayout.Label("Base URL (include /v1 if required; omit /chat/completions)");
                GUI.SetNextControlName("KSPAIHub.baseUrl"); customBaseUrl = GUILayout.TextField(customBaseUrl, 2000);
                protocolIndex = GUILayout.SelectionGrid(protocolIndex, Protocols, 3);
                discoverCustom = GUILayout.Toggle(discoverCustom, "Fetch /models (turn off for manual-only endpoints)");
            }
            if (preset == "zen")
            {
                GUILayout.Label("Zen protocol: automatic family routing, or an explicit override for this connection.");
                zenProtocolIndex = GUILayout.SelectionGrid(zenProtocolIndex, ZenProtocols, 2);
            }
            if (GUILayout.Button("Create connection")) Begin("add", client.AddProfileAsync(preset, newProfileId.Trim(),
                preset == "custom" ? customBaseUrl.Trim() : null, preset == "custom" ? Protocols[protocolIndex] : preset == "zen" && zenProtocolIndex > 0 ? Protocols[zenProtocolIndex - 1] : null,
                preset == "custom" ? (discoverCustom ? (Protocols[protocolIndex] == "messages" ? "anthropic" : "openai") : "none") : null, cancellation.Token));
        }
        private void DrawModels()
        {
            GUI.enabled = pending == null && ui.routingReady;
            if (GUILayout.Button("Refresh models (from provider)")) FetchModels(true);
            GUILayout.Label(modelsStatus + (string.IsNullOrEmpty(modelsMessage) ? "" : ": " + modelsMessage));
            if (modelIds.Length > 0 && modelsForProfile == ui.selectedProfile)
            {
                GUILayout.Label("Search models"); GUI.SetNextControlName("KSPAIHub.modelFilter"); modelFilter = GUILayout.TextField(modelFilter, 200);
                modelScroll = GUILayout.BeginScrollView(modelScroll, GUILayout.Height(155));
                int count = 0;
                for (int i = 0; i < modelIds.Length; i++)
                {
                    string label = i < modelLabels.Length ? modelLabels[i] : modelIds[i];
                    if (!string.IsNullOrWhiteSpace(modelFilter) && label.IndexOf(modelFilter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    count++;
                    if (count <= 200 && GUILayout.Button(label)) { model = modelIds[i]; status = "Model selected. Click Apply to save it."; }
                }
                if (count == 0) GUILayout.Label("No matching models. Manual IDs are still accepted.");
                if (count > 200) GUILayout.Label("Showing 200 of " + count + " matches. Refine the search to find more.");
                GUILayout.EndScrollView();
            }
            GUI.enabled = pending == null;
        }
        private void DrawGeneration()
        {
            if (ui.providerManagedOutput) GUILayout.Label("Codex/OAuth: token limit is provider-managed; the timeout still applies.");
            GUILayout.Label("Output tokens (includes provider reasoning where applicable)");
            GUI.SetNextControlName("KSPAIHub.tokens"); outputTokens = GUILayout.TextField(outputTokens, 6);
            GUILayout.Label("Maximum tokens for a bounded recovery request");
            GUI.SetNextControlName("KSPAIHub.recoveryTokens"); recoveryTokens = GUILayout.TextField(recoveryTokens, 6);
            GUILayout.Label("Request timeout seconds (1-600)");
            GUI.SetNextControlName("KSPAIHub.timeout"); generationTimeout = GUILayout.TextField(generationTimeout, 4);
            streamGeneration = GUILayout.Toggle(streamGeneration, "Receive provider output as a stream");
            GUILayout.Label("Reasoning effort (Chat/Responses; provider must support the chosen value)");
            effortIndex = GUILayout.SelectionGrid(effortIndex, Efforts, 4);
            GUILayout.Label("Thinking mode (only supported Chat APIs; leave effort at provider_default)");
            thinkingIndex = GUILayout.SelectionGrid(thinkingIndex, Thinking, 3);
            GUILayout.Label("Repetition recovery: applies only to a consumer's bounded retry, not normal requests.");
            repetitionIndex = GUILayout.SelectionGrid(repetitionIndex, Repetition, 2);
            if (GUILayout.Button("Save generation settings for this profile"))
            {
                int tokens, recovery, seconds;
                if (!int.TryParse(outputTokens, out tokens) || !int.TryParse(recoveryTokens, out recovery) || !int.TryParse(generationTimeout, out seconds))
                    status = "Enter whole numbers for token limits and timeout.";
                else Begin("generation", client.SetGenerationWithRecoveryAsync(ui.selectedProfile, tokens, recovery, seconds, streamGeneration, Efforts[effortIndex], Thinking[thinkingIndex], Repetition[repetitionIndex], cancellation.Token));
            }
        }
        private static string Arg(string value)
        {
            var b = new StringBuilder("\""); int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') b.Append('\\', slashes * 2 + 1).Append('"');
                else b.Append('\\', slashes).Append(c);
                slashes = 0;
            }
            return b.Append('\\', slashes * 2).Append('"').ToString();
        }
        private void OnDestroy()
        {
            destroyed = true; cancellation.Cancel(); cancellation.Dispose(); InputLockManager.RemoveControlLock(Lock);
            if (ownedProcess != null)
            {
                try { if (!ownedProcess.HasExited) ownedProcess.Kill(); } catch (InvalidOperationException) { }
                ownedProcess.Dispose();
            }
        }
    }
}
