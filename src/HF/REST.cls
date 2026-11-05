/// One-click web page and REST API for iris-hf-stream, served by IRIS at /hfstream/
/// (for example http://localhost:52773/hfstream/). Every route needs an IRIS user: the
/// browser shows its login box (HTTP Basic authentication), see <method>OnPreDispatch</method>.
///
/// <ul>
/// <li><code>GET  /</code>: the page</li>
/// <li><code>GET  /api/search?term=&amp;limit=</code>: <method>HF.Loader.Search</method></li>
/// <li><code>GET  /api/info?dataset=&amp;split=&amp;config=</code>: <method>HF.Loader.Info</method></li>
/// <li><code>POST /api/load</code> with a JSON body {dataset, split, target, limit, config, options}:
/// starts <method>HF.Loader.Load</method> in a background job and returns {"job": id}</li>
/// <li><code>GET  /api/load/:id</code>: state of that job and, when finished, the load result</li>
/// <li><code>GET  /api/rows?table=&amp;n=</code>: the first rows of a table</li>
/// <li><code>GET  /api/similar?table=&amp;column=&amp;q=&amp;n=</code>: semantic search on a VECTOR column</li>
/// </ul>
/// Every API answer is JSON. Loader errors keep the loader's {"status":"error",...} shape.
Class HF.REST Extends %CSP.REST
{

Parameter CONTENTTYPE = "application/json";

Parameter CHARSET = "utf-8";

/// Cross-origin requests are not answered: the API is for the page served next to it.
Parameter HandleCorsRequest = 0;

/// URL of the web application created by <method>CreateWebApp</method>.
Parameter WEBAPP = "/hfstream";

/// Background loads kept in ^HF.Jobs (older ones are removed).
Parameter KEEPJOBS = 50;

XData UrlMap [ XMLNamespace = "http://www.intersystems.com/urlmap" ]
{
<Routes>
<Route Url="/" Method="GET" Call="ServePage"/>
<Route Url="/api/search" Method="GET" Call="Search"/>
<Route Url="/api/info" Method="GET" Call="Info"/>
<Route Url="/api/load" Method="POST" Call="StartLoad"/>
<Route Url="/api/load/:id" Method="GET" Call="LoadStatus"/>
<Route Url="/api/rows" Method="GET" Call="Rows"/>
<Route Url="/api/similar" Method="GET" Call="Similar"/>
</Routes>
}

/// Creates (or updates) the /hfstream web application for <var>namespace</var>.
/// Run in any namespace; it switches to %SYS for the change.
ClassMethod CreateWebApp(namespace As %String = {$Namespace}) As %Status
{
	New $Namespace
	Set props("NameSpace") = namespace
	Set props("DispatchClass") = "HF.REST"
	Set props("Description") = "iris-hf-stream: load Hugging Face datasets with one click"
	Set props("Enabled") = 1
	Set $Namespace = "%SYS"
	// 32 + 64 = password and unauthenticated. An anonymous request only reaches
	// OnPreDispatch, which answers 401 with a Basic challenge so the browser asks for
	// an IRIS user; everything else needs that login. The database role lets the
	// anonymous request run this class's code to send the challenge.
	Set props("AutheEnabled") = 96
	Set sc = ##class(Config.Namespaces).Get(namespace, .ns)
	If $$$ISERR(sc) Quit sc
	Set props("MatchRoles") = ":%DB_" _ ns("Routines")
	If ##class(Security.Applications).Exists(..#WEBAPP) {
		Quit ##class(Security.Applications).Modify(..#WEBAPP, .props)
	}
	Quit ##class(Security.Applications).Create(..#WEBAPP, .props)
}

/// Every route needs a logged-in IRIS user: an anonymous request gets 401 and a Basic
/// challenge, which makes the browser show its login box.
ClassMethod OnPreDispatch(pUrl As %String, pMethod As %String, ByRef pContinue As %Boolean) As %Status
{
	If $Username = "UnknownUser" {
		Set pContinue = 0
		Set %response.Status = ..#HTTP401UNAUTHORIZED
		Do %response.SetHeader("WWW-Authenticate", "Basic realm=""iris-hf-stream""")
	}
	Quit $$$OK
}

/// GET /: the page, kept in the XData block below.
ClassMethod ServePage() As %Status
{
	Set %response.ContentType = "text/html"
	Set xdata = ##class(%Dictionary.CompiledXData).%OpenId(..%ClassName(1) _ "||Page")
	If '$IsObject(xdata) Quit $$$ERROR($$$GeneralError, "page not compiled")
	Do xdata.Data.Rewind()
	While 'xdata.Data.AtEnd { Write xdata.Data.Read(32000) }
	Quit $$$OK
}

ClassMethod Search() As %Status
{
	Set limit = +%request.Get("limit", 20)
	Set:(limit < 1)||(limit > 100) limit = 20
	Write ##class(HF.Loader).Search(%request.Get("term"), limit)
	Quit $$$OK
}

ClassMethod Info() As %Status
{
	Set split = %request.Get("split")
	Set:split="" split = "train"
	// A smaller sample than the default keeps the preview quick; types are re-inferred on load.
	Write ##class(HF.Loader).Info(%request.Get("dataset"), split, %request.Get("config"), 200)
	Quit $$$OK
}

/// POST /api/load: starts the load in a background job, so a long load does not hit the
/// web gateway timeout. The page then polls /api/load/:id.
ClassMethod StartLoad() As %Status
{
	// Only JSON bodies: a plain HTML form from another site cannot start a load.
	If $Piece(%request.ContentType, ";") '= "application/json" {
		Quit ..Fail(415, "send the request as application/json")
	}
	Try {
		Set body = {}.%FromJSON(%request.Content)
	} Catch {
		Set body = ""
	}
	If '$IsObject(body) Quit ..Fail(400, "the body is not a JSON object")
	If (body.dataset = "") || (body.target = "") Quit ..Fail(400, "dataset and target are required")

	Set id = $Increment(^HF.Jobs)
	Set ^HF.Jobs(id, "request") = body.%ToJSON()
	Set ^HF.Jobs(id, "start") = $ZHorolog
	Set ^HF.Jobs(id, "base") = ..Count(body.target)
	Kill ^HF.Jobs(id - ..#KEEPJOBS)
	Job ##class(HF.REST).RunLoad(id)::10
	If '$Test {
		Kill ^HF.Jobs(id)
		Quit ..Fail(503, "could not start a background job")
	}
	Write {"job": (id)}.%ToJSON()
	Quit $$$OK
}

/// Body of the background job started by <method>StartLoad</method>.
ClassMethod RunLoad(id As %Integer)
{
	Set result = ""
	Try {
		Set req = {}.%FromJSON(^HF.Jobs(id, "request"))
		Set options = req.%Get("options")
		Set:'$IsObject(options) options = ""
		Set result = ##class(HF.Loader).Load(req.dataset, req.split, req.target, +req.limit, req.config, 1000, options)
	} Catch ex {
		Set result = {"status": "error", "code": "INTERNAL", "message": (ex.DisplayString())}.%ToJSON()
	}
	Set ^HF.Jobs(id, "seconds") = $ZHorolog - $Get(^HF.Jobs(id, "start"), $ZHorolog)
	Set ^HF.Jobs(id, "result") = result
}

/// GET /api/load/:id: {"job", "state": "running"|"done", "seconds", "rows_so_far"?, "result"?}
ClassMethod LoadStatus(id As %String) As %Status
{
	If (id '= +id) || '$Data(^HF.Jobs(id, "request")) Quit ..Fail(404, "no such load: " _ id)
	Set out = {"job": (+id)}
	If $Data(^HF.Jobs(id, "result")) {
		Set out.state = "done"
		Set out.seconds = $FNumber(^HF.Jobs(id, "seconds"), "", 1)
		Set out.result = {}.%FromJSON(^HF.Jobs(id, "result"))
	} Else {
		Set out.state = "running"
		Set out.seconds = $FNumber($ZHorolog - ^HF.Jobs(id, "start"), "", 1)
		// Approximate progress: rows already in the table this load writes to.
		Set req = {}.%FromJSON(^HF.Jobs(id, "request"))
		Set table = req.target
		Set mode = $Select($IsObject(req.options): req.options.mode, 1: "")
		If mode = "replace" Set table = table _ "_hfstream_tmp"
		Set rows = ..Count(table)
		If (rows '= "") && (mode = "append") Set rows = rows - +^HF.Jobs(id, "base")
		Do:rows'="" out.%Set("rows_so_far", +rows, "number")
	}
	Write out.%ToJSON()
	Quit $$$OK
}

/// GET /api/rows?table=Schema.Table&amp;n=5: first rows; long text cut to 300 characters,
/// VECTOR columns left out.
ClassMethod Rows() As %Status
{
	Set table = %request.Get("table")
	Set n = +%request.Get("n", 5)
	Set:(n < 1)||(n > 100) n = 5
	Set cols = ..Columns(table, .vectors)
	If cols = "" Quit ..Fail(404, "no such table: " _ table)
	Quit ..Query("SELECT TOP " _ n _ " " _ cols _ " FROM " _ table, "")
}

/// GET /api/similar?table=&amp;column=text_embedding&amp;q=...&amp;n=5: the rows closest in meaning
/// to q, with the score and the SQL that was run.
ClassMethod Similar() As %Status
{
	Set table = %request.Get("table"), column = %request.Get("column"), q = %request.Get("q")
	Set n = +%request.Get("n", 5)
	Set:(n < 1)||(n > 100) n = 5
	Set cols = ..Columns(table, .vectors)
	If cols = "" Quit ..Fail(404, "no such table: " _ table)
	If '$Data(vectors(column)) Quit ..Fail(400, "no VECTOR column " _ column _ " in " _ table)
	If q = "" Quit ..Fail(400, "q (the search text) is required")
	Set vector = "", problem = ""
	Try {
		Set vector = ##class(HF.Loader).Embed(q)
	} Catch ex {
		Set problem = ex.DisplayString()
	}
	If vector = "" Quit ..Fail(500, "could not embed the search text " _ problem)
	Set dim = $Length(vector, ",")
	Set score = "VECTOR_COSINE(" _ column _ ", TO_VECTOR(?, FLOAT, " _ dim _ "))"
	Set sql = "SELECT TOP " _ n _ " ROUND(" _ score _ ", 3) AS score, " _ cols _ " FROM " _ table _ " ORDER BY score DESC"
	Set shown = "SELECT TOP " _ n _ " ... FROM " _ table _ $Char(10) _ "ORDER BY VECTOR_COSINE(" _ column
		_ ", TO_VECTOR(HF.Embed('" _ $Replace(q, "'", "''") _ "'), FLOAT, " _ dim _ ")) DESC"
	Quit ..Query(sql, shown, vector)
}

/// Runs <var>sql</var> and writes {"columns": [...], "rows": [[...], ...], "sql": shown}.
ClassMethod Query(sql As %String, shown As %String, args...) As %Status [ Private ]
{
	Set stmt = ##class(%SQL.Statement).%New()
	Set sc = stmt.%Prepare(sql)
	If $$$ISERR(sc) Quit ..Fail(500, $System.Status.GetErrorText(sc))
	Set rs = stmt.%Execute(args...)
	If rs.%SQLCODE < 0 Quit ..Fail(500, "SQLCODE " _ rs.%SQLCODE _ ": " _ rs.%Message)
	Set meta = rs.%GetMetadata(), columns = []
	For i = 1:1:meta.columnCount { Do columns.%Push(meta.columns.GetAt(i).colName) }
	Set rows = []
	While rs.%Next() {
		Set row = []
		For i = 1:1:meta.columnCount { Do row.%Push(rs.%GetData(i)) }
		Do rows.%Push(row)
	}
	Set out = {"columns": (columns), "rows": (rows)}
	Set:shown'="" out.sql = shown
	Write out.%ToJSON()
	Quit $$$OK
}

/// Select list for <var>table</var>: long text through SUBSTRING, VECTOR columns left out and
/// returned in <var>vectors</var>. "" if the name is not a schema.table of an existing table.
ClassMethod Columns(table As %String, Output vectors) As %String [ Private ]
{
	Kill vectors
	If table '?1A.(1AN,1"_")1"."1A.(1AN,1"_") Quit ""
	Set sql = "SELECT p.SqlFieldName, p.Type FROM %Dictionary.CompiledProperty p"
		_ " JOIN INFORMATION_SCHEMA.TABLES t ON p.parent = t.CLASSNAME"
		_ " WHERE t.TABLE_SCHEMA = ? AND t.TABLE_NAME = ? AND p.SqlColumnNumber IS NOT NULL"
		_ " ORDER BY p.SqlColumnNumber"
	Set rs = ##class(%SQL.Statement).%ExecDirect(, sql, $Piece(table, "."), $Piece(table, ".", 2))
	Set list = ""
	While rs.%Next() {
		Set name = rs.SqlFieldName, type = rs.Type
		If type = "%Library.Vector" { Set vectors(name) = "" Continue }
		Set expr = $Select(type [ "Stream": "SUBSTRING(" _ name _ ", 1, 300) AS " _ name, 1: name)
		Set list = list _ $Select(list = "": "", 1: ", ") _ expr
	}
	Quit list
}

/// Row count of <var>table</var>, or "" if it does not exist (yet) or the name is not valid.
ClassMethod Count(table As %String) As %String [ Private ]
{
	If table '?1A.(1AN,1"_")1"."1A.(1AN,1"_") Quit ""
	Try {
		Set rs = ##class(%SQL.Statement).%ExecDirect(, "SELECT COUNT(*) FROM " _ table)
		Quit:rs.%SQLCODE<0
		Set count = $Select(rs.%Next(): rs.%GetData(1), 1: "")
	} Catch {
	}
	Quit $Get(count)
}

ClassMethod Fail(httpStatus As %Integer, message As %String) As %Status [ Private ]
{
	Set %response.Status = httpStatus _ " " _ $Case(httpStatus, 400: "Bad Request", 404: "Not Found",
		415: "Unsupported Media Type", 503: "Service Unavailable", : "Internal Server Error")
	Write {"status": "error", "code": "REQUEST", "message": (message)}.%ToJSON()
	Quit $$$OK
}

/// The page. Plain HTML and JavaScript, no external files. Every line is indented so that
/// no line of the page is a lone closing brace (which would end this block).
XData Page [ MimeType = text/html ]
{
  <!doctype html>
  <html lang="en">
  <head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iris-hf-stream</title>
  <style>
    :root { --bg:#f6f7f9; --card:#fff; --text:#1c2430; --muted:#5f6b7a; --line:#dde2e8;
            --accent:#2f6fdf; --accent-text:#fff; --ok:#1d7f4e; --err:#b42318; --code:#f0f2f5; }
    @media (prefers-color-scheme: dark) {
      :root { --bg:#0f1520; --card:#18202d; --text:#e7ecf3; --muted:#9aa7b8; --line:#2c3748;
              --accent:#5b8ff0; --accent-text:#0b1220; --ok:#4cc38a; --err:#f07167; --code:#111925; }
    }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text);
           font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
    main { max-width:960px; margin:0 auto; padding:24px 16px 48px; }
    h1 { margin:0 0 4px; font-size:26px; }
    h2 { margin:0 0 12px; font-size:17px; }
    p.lead { margin:0 0 20px; color:var(--muted); }
    section { background:var(--card); border:1px solid var(--line); border-radius:10px;
              padding:16px; margin-bottom:16px; }
    label { display:block; font-size:13px; color:var(--muted); margin-bottom:4px; }
    input, select { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:6px;
                    background:var(--bg); color:var(--text); font:inherit; }
    button { padding:8px 16px; border:0; border-radius:6px; background:var(--accent);
             color:var(--accent-text); font:inherit; font-weight:600; cursor:pointer; white-space:nowrap; }
    button:disabled { opacity:.5; cursor:default; }
    button.big { font-size:17px; padding:12px 28px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    .row > div { flex:1 1 180px; min-width:0; }
    .inline { display:flex; gap:8px; }
    .inline input { flex:1; }
    .radios { display:flex; gap:16px; flex-wrap:wrap; padding-top:6px; }
    .radios label, .check { display:inline-flex; gap:6px; align-items:center; color:var(--text);
                            font-size:15px; margin:0; }
    .radios input, .check input { width:auto; }
    .muted { color:var(--muted); font-size:13px; }
    .ok { color:var(--ok); font-weight:600; }
    .err { color:var(--err); font-weight:600; }
    ul.results { list-style:none; margin:12px 0 0; padding:0; }
    ul.results li { padding:8px 10px; border:1px solid var(--line); border-radius:6px; margin-bottom:6px;
                    cursor:pointer; display:flex; justify-content:space-between; gap:12px; }
    ul.results li:hover, ul.results li.sel { border-color:var(--accent); }
    .chips span { display:inline-block; background:var(--code); border-radius:12px; padding:2px 10px;
                  margin:0 6px 6px 0; font-size:13px; }
    pre { background:var(--code); padding:10px; border-radius:6px; overflow-x:auto; font-size:13px;
          margin:8px 0 0; white-space:pre-wrap; word-break:break-word; }
    .table-wrap { overflow-x:auto; margin-top:10px; }
    table { border-collapse:collapse; width:100%; font-size:13px; }
    th, td { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:600; }
    td { max-width:420px; overflow-wrap:anywhere; }
    .hidden { display:none; }
    .bar { height:6px; background:var(--code); border-radius:3px; overflow:hidden; margin-top:8px; }
    .bar div { height:100%; width:30%; background:var(--accent); animation:slide 1.2s infinite ease-in-out; }
    @keyframes slide { from { margin-left:-30%; } to { margin-left:100%; } }
  </style>
  </head>
  <body>
  <main>
    <h1>iris-hf-stream</h1>
    <p class="lead">Load a Hugging Face dataset into an InterSystems IRIS table with one click.</p>

    <section>
      <h2>1. Find a dataset</h2>
      <form id="searchForm" class="inline">
        <input id="term" placeholder="Search the Hugging Face Hub, e.g. sentiment" autocomplete="off">
        <button id="searchBtn">Search</button>
      </form>
      <div id="searchMsg" class="muted"></div>
      <ul id="results" class="results"></ul>
    </section>

    <section>
      <h2>2. Choose what to load</h2>
      <div class="row">
        <div><label for="dataset">Dataset (owner/name)</label>
          <div class="inline"><input id="dataset" placeholder="stanfordnlp/imdb">
          <button id="previewBtn" type="button">Preview</button></div></div>
      </div>
      <div class="row" style="margin-top:12px">
        <div><label for="config">Configuration (only if the dataset has several)</label><input id="config"></div>
        <div><label for="split">Split</label><select id="split"><option>train</option></select></div>
      </div>
      <div id="previewMsg" class="muted" style="margin-top:8px"></div>
      <div id="previewCols" class="chips" style="margin-top:8px"></div>
      <div class="row" style="margin-top:12px">
        <div><label for="target">IRIS table (Schema.Table)</label><input id="target" placeholder="Demo.Imdb"></div>
        <div><label for="limit">Rows (0 = all)</label><input id="limit" type="number" min="0" value="1000"></div>
      </div>
      <div class="row" style="margin-top:12px">
        <div><label>If the table exists</label>
          <div class="radios">
            <label><input type="radio" name="mode" value="fail" checked> stop</label>
            <label><input type="radio" name="mode" value="replace"> replace it</label>
            <label><input type="radio" name="mode" value="append"> append</label>
          </div></div>
        <div><label for="embed">Embeddings for semantic search</label>
          <select id="embed"><option value="">none</option></select></div>
      </div>
    </section>

    <section>
      <h2>3. Load</h2>
      <button id="loadBtn" class="big">Load into IRIS</button>
      <div id="loadMsg" style="margin-top:10px"></div>
      <div id="loadBar" class="bar hidden"><div></div></div>
      <div id="sqlBox" class="hidden"><div class="muted" style="margin-top:12px">The same load as one line of SQL:</div><pre id="sqlLine"></pre></div>
      <div id="rowsBox" class="table-wrap"></div>
    </section>

    <section id="similarSection" class="hidden">
      <h2>4. Search by meaning</h2>
      <form id="similarForm" class="inline">
        <input id="q" placeholder="e.g. a heartwarming family film that made me cry" autocomplete="off">
        <button id="similarBtn">Search</button>
      </form>
      <pre id="similarSql" class="hidden"></pre>
      <div id="similarBox" class="table-wrap"></div>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    let loaded = null;

    // Built from origin + path so that a page opened as http://user:pass@host/ still works.
    const base = location.origin + location.pathname.replace(/[^/]*$/, "");
    async function api(path, options) {
      let response;
      try { response = await fetch(base + "api/" + path, options); }
      catch (e) { return { status: "error", message: "Cannot reach IRIS: " + e.message }; }
      let body;
      try { body = await response.json(); } catch (e) { body = { status: "error", message: "HTTP " + response.status }; }
      if (!response.ok && !body.message) { body.message = "HTTP " + response.status; }
      return body;
    }
    function text(el, value, cls) { el.textContent = value; el.className = cls || ""; }
    function q(params) { return new URLSearchParams(params).toString(); }

    function tableName(dataset) {
      const name = dataset.split("/").pop().replace(/[^A-Za-z0-9]+/g, " ").trim();
      let pascal = name.split(" ").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join("");
      if (!/^[A-Za-z]/.test(pascal)) { pascal = "T" + pascal; }
      return "Demo." + (pascal || "Dataset");
    }

    function renderTable(box, data) {
      box.textContent = "";
      if (!data.rows || !data.rows.length) { box.textContent = "No rows."; return; }
      const table = document.createElement("table");
      const head = table.insertRow();
      data.columns.forEach(c => { const th = document.createElement("th"); th.textContent = c; head.appendChild(th); });
      data.rows.forEach(r => {
        const tr = table.insertRow();
        r.forEach(v => { tr.insertCell().textContent = v === null ? "" : String(v); });
      });
      box.appendChild(table);
    }

    $("searchForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const term = $("term").value.trim();
      if (!term) { return; }
      $("searchBtn").disabled = true;
      text($("searchMsg"), "Searching the Hub...", "muted");
      $("results").textContent = "";
      const out = await api("search?" + q({ term: term, limit: 10 }));
      $("searchBtn").disabled = false;
      if (out.status !== "ok") { text($("searchMsg"), out.message, "err"); return; }
      text($("searchMsg"), out.results.length ? "Most downloaded first. Click one to preview it." : "Nothing found.", "muted");
      out.results.forEach(r => {
        const li = document.createElement("li");
        const name = document.createElement("span"); name.textContent = r.id;
        const stats = document.createElement("span"); stats.className = "muted";
        stats.textContent = r.downloads.toLocaleString("en-US") + " downloads";
        li.append(name, stats);
        li.addEventListener("click", () => {
          document.querySelectorAll("#results li").forEach(x => x.classList.remove("sel"));
          li.classList.add("sel");
          $("dataset").value = r.id;
          $("config").value = "";
          preview(true);
        });
        $("results").appendChild(li);
      });
    });

    $("previewBtn").addEventListener("click", () => preview(true));
    $("split").addEventListener("change", () => preview(false));

    async function preview(resetSplits) {
      const dataset = $("dataset").value.trim();
      if (!dataset) { return; }
      $("target").value = tableName(dataset);
      $("previewCols").textContent = "";
      text($("previewMsg"), "Reading a sample of " + dataset + "...", "muted");
      const params = { dataset: dataset, config: $("config").value.trim() };
      if (!resetSplits) { params.split = $("split").value; }
      const out = await api("info?" + q(params));
      if (out.status !== "ok") { text($("previewMsg"), out.message, "err"); return; }
      if (resetSplits) {
        const splits = Object.keys(out.splits || {});
        if (!splits.length) { splits.push(out.split); }
        $("split").textContent = "";
        splits.forEach(s => {
          const o = document.createElement("option"); o.value = s;
          o.textContent = s + (out.splits && out.splits[s] ? " (" + out.splits[s].toLocaleString("en-US") + " rows)" : "");
          if (s === out.split) { o.selected = true; }
          $("split").appendChild(o);
        });
        if (splits.length > 1) {
          const all = document.createElement("option"); all.value = "all"; all.textContent = "all splits";
          $("split").appendChild(all);
        }
      }
      $("embed").textContent = "";
      const none = document.createElement("option"); none.value = ""; none.textContent = "none";
      $("embed").appendChild(none);
      out.columns.forEach(c => {
        const chip = document.createElement("span"); chip.textContent = c.name + " " + c.type;
        $("previewCols").appendChild(chip);
        if (/VARCHAR/.test(c.type) && !/_name$/.test(c.name)) {
          const o = document.createElement("option"); o.value = c.source; o.textContent = c.source;
          $("embed").appendChild(o);
        }
      });
      let msg = "Columns the table will get:";
      if (out.skipped_columns && out.skipped_columns.length) {
        msg += " (left out: " + out.skipped_columns.join(", ") + ")";
      }
      text($("previewMsg"), msg, "muted");
    }

    function sqlLine(req) {
      const s = (v) => "'" + String(v).replace(/'/g, "''") + "'";
      let line = "CALL HF.LoadDataset(" + s(req.dataset) + ", " + s(req.split) + ", " + s(req.target) + ", " + req.limit;
      const opts = Object.keys(req.options).length ? JSON.stringify(req.options) : "";
      if (req.config || opts) { line += ", " + s(req.config); }
      if (opts) { line += ", 1000, " + s(opts); }
      return line + ")";
    }

    $("loadBtn").addEventListener("click", async () => {
      const req = {
        dataset: $("dataset").value.trim(), split: $("split").value, target: $("target").value.trim(),
        limit: parseInt($("limit").value || "0", 10), config: $("config").value.trim(), options: {}
      };
      if (!req.dataset || !req.target) { text($("loadMsg"), "Choose a dataset and a table name first.", "err"); return; }
      const mode = document.querySelector("input[name=mode]:checked").value;
      if (mode !== "fail") { req.options.mode = mode; }
      if ($("embed").value) { req.options.embed = $("embed").value; }
      $("loadBtn").disabled = true;
      $("rowsBox").textContent = "";
      $("similarSection").classList.add("hidden");
      $("sqlLine").textContent = sqlLine(req);
      $("sqlBox").classList.remove("hidden");
      $("loadBar").classList.remove("hidden");
      text($("loadMsg"), "Starting...", "muted");
      const started = await api("load", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) });
      if (!started.job) { finish({ status: "error", message: started.message }, req); return; }
      const timer = setInterval(async () => {
        const st = await api("load/" + started.job);
        if (st.state === "running") {
          const rows = st.rows_so_far !== undefined ? ", " + st.rows_so_far.toLocaleString("en-US") + " rows so far" : "";
          text($("loadMsg"), "Loading... " + st.seconds + " s" + rows, "muted");
          return;
        }
        clearInterval(timer);
        finish(st.state === "done" ? st.result : { status: "error", message: st.message }, req);
      }, 1000);
    });

    async function finish(result, req) {
      $("loadBtn").disabled = false;
      $("loadBar").classList.add("hidden");
      if (result.status !== "ok") {
        let error = result.message || "The load failed.";
        if (/already exists/.test(error)) { error = "Table " + req.target + " already exists. Choose \"replace it\" or \"append\" above, or another table name."; }
        text($("loadMsg"), error, "err");
        return;
      }
      let msg = "Loaded " + result.rows_loaded.toLocaleString("en-US") + " rows into " + result.table + " in " + result.seconds + " s.";
      if (result.widened_columns && result.widened_columns.length) { msg += " Widened: " + result.widened_columns.join(", ") + "."; }
      if (result.skipped_columns && result.skipped_columns.length) { msg += " Left out: " + result.skipped_columns.join(", ") + "."; }
      text($("loadMsg"), msg, "ok");
      renderTable($("rowsBox"), await api("rows?" + q({ table: result.table, n: 5 })));
      const vector = result.columns.find(c => /^VECTOR/.test(c.type));
      loaded = vector ? { table: result.table, column: vector.name } : null;
      $("similarSection").classList.toggle("hidden", !loaded);
      $("similarBox").textContent = "";
      $("similarSql").classList.add("hidden");
    }

    $("similarForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const text_ = $("q").value.trim();
      if (!text_ || !loaded) { return; }
      $("similarBtn").disabled = true;
      const out = await api("similar?" + q({ table: loaded.table, column: loaded.column, q: text_, n: 5 }));
      $("similarBtn").disabled = false;
      if (!out.rows) { $("similarBox").textContent = out.message; return; }
      $("similarSql").textContent = out.sql;
      $("similarSql").classList.remove("hidden");
      renderTable($("similarBox"), out);
    });
  </script>
  </body>
  </html>
}

}
