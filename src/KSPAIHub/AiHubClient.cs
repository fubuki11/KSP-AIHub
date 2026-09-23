using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace KSPAIHub.API
{
    [Serializable] public sealed class HubConnection { public string endpoint = ""; public string token = ""; }
    [Serializable] public sealed class AiMessage
    {
        public string role, content;
        public AiMessage(string role, string content) { this.role = role; this.content = content; }
    }
    // Flat DTOs can be parsed with JsonUtility on the Unity main thread.
    [Serializable] public sealed class HubReply
    {
        public bool ok = false;
        public string code = "", message = "", text = "", jsonText = "", model = "", provider = "", profile = "", requestId = "";
        public int inputTokens = -1, outputTokens = -1;
        public int outputLimit = -1;
        public bool providerManagedOutput = false;
    }
    [Serializable] public sealed class HubUi
    {
        public bool ok = false;
        public string message = "", clientId = "", selectedProfile = "", model = "", credential = "", authState = "", authKind = "", authorizationUrl = "";
        public bool routingReady = false, inheritsGlobal = false, hasOverride = false;
        public string scope = "", selectionSource = "", routeError = "";
        public string[] clientIds = new string[0];
        public string[] profileIds = new string[0], profileLabels = new string[0];
        public string[] presetIds = new string[0], presetLabels = new string[0];
        public string[] modelIds = new string[0], modelLabels = new string[0], modelProtocols = new string[0];
        public string profile = "", baseUrl = "", protocol = "", modelsStatus = "", modelsMessage = "";
        public bool cached = false;
        public long fetchedAt = 0;
        public int maxOutputTokens = 0, recoveryMaxOutputTokens = 0;
        public double timeoutSeconds = 0;
        public bool streamEnabled = false, providerManagedOutput = false;
        public string reasoningEffort = "provider_default", thinkingMode = "provider_default";
        public string repetitionRecovery = "prompt_only";
    }

    public sealed class AiHubClient
    {
        private readonly string endpoint, token;
        public string ClientId { get; private set; }
        public AiHubClient(string endpoint, string token, string clientId = null)
        {
            Uri uri;
            if (!Uri.TryCreate(endpoint, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1" ||
                uri.Port < 1 || uri.AbsolutePath != "/" || uri.Query != "" || uri.Fragment != "" || uri.UserInfo != "")
                throw new ArgumentException("AI Hub endpoint must be literal IPv4 loopback HTTP.");
            if (string.IsNullOrEmpty(token) || token.Length < 32 || token.Any(c => c < 33 || c > 126)) throw new ArgumentException("Invalid IPC token.");
            if (clientId != null && (clientId.Length == 0 || clientId.Length > 80 || clientId.Any(c =>
                !(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z') && !(c >= '0' && c <= '9') && c != '_' && c != '-' && c != '.')))
                throw new ArgumentException("Invalid client ID.");
            this.endpoint = endpoint.TrimEnd('/'); this.token = token; ClientId = clientId;
        }

        // Call on the game main thread because JsonUtility is a Unity API.
        public static AiHubClient FromConnectionFile(string path, string clientId = null)
        {
            var connection = JsonUtility.FromJson<HubConnection>(File.ReadAllText(path, Encoding.UTF8));
            return new AiHubClient(connection.endpoint, connection.token, clientId);
        }
        public Task<string> GetUiAsync(CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("GET", ClientId == null ? "/v1/ui" : "/v1/ui?clientId=" + Uri.EscapeDataString(ClientId), null, cancellation);
        }
        public Task<string> SelectAsync(string profile, string model, CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("POST", "/v1/select", "{" + SelectionScope() + ",\"profile\":" + Quote(profile) + ",\"model\":" + Quote(model) + "}", cancellation);
        }
        public Task<string> InheritDefaultsAsync(CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("POST", "/v1/select", "{" + SelectionScope() + ",\"inherit\":true}", cancellation);
        }
        private string SelectionScope()
        {
            return ClientId == null ? "\"scope\":\"global\"" : "\"scope\":\"client\",\"clientId\":" + Quote(ClientId);
        }
        public Task<string> BeginLoginAsync(string credential, CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("POST", "/v1/auth/begin", "{\"credential\":" + Quote(credential) + "}", cancellation);
        }
        public Task<string> StoreApiKeyAsync(string credential, string key, CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("POST", "/v1/auth/api-key", "{\"credential\":" + Quote(credential) + ",\"apiKey\":" + Quote(key) + "}", cancellation);
        }
        public Task<string> GetModelsAsync(string profile, bool refresh = false, CancellationToken cancellation = default(CancellationToken))
        {
            return RequestAsync("GET", "/v1/models?profile=" + Uri.EscapeDataString(profile) + "&refresh=" + (refresh ? "true" : "false"), null, cancellation);
        }
        public Task<string> SetGenerationAsync(string profile, int tokens, int recoveryTokens, int timeoutSeconds, bool stream,
            string effort, string thinking, CancellationToken cancellation = default(CancellationToken))
        {
            string body = "{\"profile\":" + Quote(profile) + ",\"settings\":{\"maxOutputTokens\":" + tokens +
                ",\"recoveryMaxOutputTokens\":" + recoveryTokens + ",\"timeout\":" + timeoutSeconds +
                ",\"stream\":" + (stream ? "true" : "false") + ",\"reasoningEffort\":" + Quote(effort) + ",\"thinkingMode\":" + Quote(thinking) + "}}";
            return RequestAsync("POST", "/v1/generation-settings", body, cancellation);
        }
        public Task<string> SetGenerationWithRecoveryAsync(string profile, int tokens, int recoveryTokens, int timeoutSeconds, bool stream,
            string effort, string thinking, string repetition, CancellationToken cancellation = default(CancellationToken))
        {
            string body = "{\"profile\":" + Quote(profile) + ",\"settings\":{\"maxOutputTokens\":" + tokens +
                ",\"recoveryMaxOutputTokens\":" + recoveryTokens + ",\"timeout\":" + timeoutSeconds +
                ",\"stream\":" + (stream ? "true" : "false") + ",\"reasoningEffort\":" + Quote(effort) +
                ",\"thinkingMode\":" + Quote(thinking) + ",\"repetitionRecovery\":" + Quote(repetition) + "}}";
            return RequestAsync("POST", "/v1/generation-settings", body, cancellation);
        }
        public Task<string> AddProfileAsync(string preset, string id, string baseUrl, string protocol, string modelsFormat,
            CancellationToken cancellation = default(CancellationToken))
        {
            string body = "{\"preset\":" + Quote(preset) + ",\"id\":" + Quote(id) +
                (baseUrl == null ? "" : ",\"baseUrl\":" + Quote(baseUrl)) +
                (protocol == null ? "" : ",\"protocol\":" + Quote(protocol)) +
                (modelsFormat == null ? "" : ",\"modelsFormat\":" + Quote(modelsFormat)) + "}";
            return RequestAsync("POST", "/v1/profiles", body, cancellation);
        }
        public Task<string> GenerateAsync(IEnumerable<AiMessage> messages, string format = "text", string profile = null, string model = null,
            CancellationToken cancellation = default(CancellationToken))
        {
            if (format != "text" && format != "json") throw new ArgumentException("format must be text/json.");
            var entries = messages.ToArray();
            if (entries.Length < 1 || entries.Length > 100 || entries.Any(m => m == null ||
                (m.role != "system" && m.role != "user" && m.role != "assistant") || m.content == null)) throw new ArgumentException("Invalid text messages.");
            string body = "{\"format\":" + Quote(format) + (ClientId == null ? "" : ",\"clientId\":" + Quote(ClientId)) +
                (profile == null ? "" : ",\"profile\":" + Quote(profile)) + (model == null ? "" : ",\"model\":" + Quote(model)) +
                ",\"messages\":[" + string.Join(",", entries.Select(m => "{\"role\":" + Quote(m.role) + ",\"content\":" + Quote(m.content) + "}")) + "]}";
            return RequestAsync("POST", "/v1/generate", body, cancellation);
        }

        private Task<string> RequestAsync(string method, string path, string body, CancellationToken cancellation)
        {
            return Task.Run(() => {
                cancellation.ThrowIfCancellationRequested();
                var request = (HttpWebRequest)WebRequest.Create(endpoint + path);
                request.Method = method; request.Proxy = null; request.AllowAutoRedirect = false;
                request.Timeout = 630000; request.ReadWriteTimeout = 630000; request.KeepAlive = false;
                request.Headers[HttpRequestHeader.Authorization] = "Bearer " + token;
                using (cancellation.Register(request.Abort))
                {
                    try
                    {
                        if (body != null)
                        {
                            byte[] bytes = Encoding.UTF8.GetBytes(body);
                            if (bytes.Length > 350000) throw new InvalidOperationException("AI Hub request exceeds its size limit.");
                            request.ContentType = "application/json"; request.ContentLength = bytes.Length;
                            using (var stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
                        }
                        using (var response = (HttpWebResponse)request.GetResponse()) return Read(response);
                    }
                    catch (WebException error)
                    {
                        cancellation.ThrowIfCancellationRequested();
                        if (error.Response != null) { using (var response = error.Response) return Read(response); }
                        throw new IOException("AI Hub is unavailable. Start/configure its local service.");
                    }
                }
            }, cancellation);
        }
        private static string Read(WebResponse response)
        {
            using (var stream = response.GetResponseStream())
            using (var reader = new StreamReader(stream, Encoding.UTF8))
            {
                var result = new StringBuilder(); var buffer = new char[4096]; int count;
                while ((count = reader.Read(buffer, 0, buffer.Length)) > 0)
                {
                    if (result.Length + count > 4500000) throw new IOException("AI Hub response exceeds its size limit.");
                    result.Append(buffer, 0, count);
                }
                return result.ToString();
            }
        }
        private static string Quote(string value)
        {
            if (value == null) return "null";
            var b = new StringBuilder("\"");
            foreach (char c in value)
            {
                if (c == '"' || c == '\\') b.Append('\\').Append(c);
                else if (c < 32 || char.IsSurrogate(c)) b.Append("\\u").Append(((int)c).ToString("x4", System.Globalization.CultureInfo.InvariantCulture));
                else b.Append(c);
            }
            return b.Append('"').ToString();
        }
    }
}
