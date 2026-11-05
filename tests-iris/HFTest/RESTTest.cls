/// Tests of the /hfstream web page and REST API (HF.REST), through the IRIS web server
/// inside the container, as a browser would call them. Needs network access to the Hub.
/// Uses the development image's default account (_SYSTEM / SYS).
Class HFTest.RESTTest Extends %UnitTest.TestCase
{

Parameter TABLES = "HFTest.Web,HFTest.WebVec";

Parameter USER = "_SYSTEM";

Parameter PASSWORD = "SYS";

Method OnBeforeOneTest(testname As %String) As %Status
{
	Quit ..DropAll()
}

Method OnAfterAllTests() As %Status
{
	Quit ..DropAll()
}

Method DropAll() As %Status
{
	For i = 1:1:$Length(..#TABLES, ",") {
		Do ##class(%SQL.Statement).%ExecDirect(, "DROP TABLE " _ $Piece(..#TABLES, ",", i))
	}
	Quit $$$OK
}

/// Sends one request to the local web server. Returns the HTTP status; the body is in <var>body</var>.
Method Call(method As %String, path As %String, Output body, json As %String = "", login As %Boolean = 1, contentType As %String = "application/json") As %Integer
{
	Set http = ##class(%Net.HttpRequest).%New()
	Set http.Server = "localhost", http.Port = 52773, http.Timeout = 120
	If login Set http.Username = ..#USER, http.Password = ..#PASSWORD
	If json '= "" {
		Set http.ContentType = contentType
		Do http.EntityBody.Write(json)
	}
	Set sc = http.Send(method, ##class(HF.REST).#WEBAPP _ path)
	If $$$ISERR(sc) Set body = $System.Status.GetErrorText(sc) Quit 0
	Set body = http.HttpResponse.Data
	Set:$IsObject(body) body = body.Read($$$MaxStringLength)
	Quit http.HttpResponse.StatusCode
}

/// GET that must answer 200 with JSON; returns the parsed object.
Method GetJson(path As %String) As %DynamicAbstractObject
{
	Set code = ..Call("GET", path, .body)
	Do $$$AssertEquals(code, 200, path _ " -> " _ $Extract(body, 1, 300))
	Try { Set out = {}.%FromJSON(body) } Catch { Set out = {} }
	Quit out
}

/// Starts a load through the API and waits (up to two minutes) for its result.
Method LoadAndWait(request As %DynamicObject) As %DynamicObject
{
	Set code = ..Call("POST", "/api/load", .body, request.%ToJSON())
	Do $$$AssertEquals(code, 200, "start load: " _ body)
	Set job = {}.%FromJSON(body).job
	Do $$$AssertTrue(job > 0, "job id")
	For i = 1:1:240 {
		Set st = ..GetJson("/api/load/" _ job)
		Quit:st.state'="running"
		Hang 0.5
	}
	Do $$$AssertEquals(st.state, "done", st.%ToJSON())
	Quit st.result
}

Method TestPageNeedsLogin()
{
	Do $$$AssertEquals(..Call("GET", "/", .body, , 0), 401, "no password")
	Do $$$AssertEquals(..Call("GET", "/", .body), 200)
	Do $$$AssertTrue(body [ "Load into IRIS", "the page is served")
}

Method TestSearchAndInfo()
{
	Set out = ..GetJson("/api/search?term=rotten_tomatoes&limit=3")
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Set out = ..GetJson("/api/info?dataset=cornell-movie-review-data/rotten_tomatoes")
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertTrue(out.splits.validation > 0, "split sizes for the split list")
}

Method TestLoadInBackgroundThenRows()
{
	Set result = ..LoadAndWait({"dataset": "cornell-movie-review-data/rotten_tomatoes", "split": "train", "target": "HFTest.Web", "limit": 20})
	Do $$$AssertEquals(result.status, "ok", result.%ToJSON())
	Do $$$AssertEquals(result."rows_loaded", 20)
	Set rows = ..GetJson("/api/rows?table=HFTest.Web&n=3")
	Do $$$AssertEquals(rows.rows.%Size(), 3)
	Do $$$AssertEquals(rows.columns.%Get(0), "text")
	// The loader's own errors come back as the load result.
	Set again = ..LoadAndWait({"dataset": "cornell-movie-review-data/rotten_tomatoes", "split": "train", "target": "HFTest.Web", "limit": 20})
	Do $$$AssertEquals(again.code, "VALIDATION", again.%ToJSON())
}

Method TestSemanticSearch()
{
	Set request = {"dataset": "cornell-movie-review-data/rotten_tomatoes", "split": "test", "target": "HFTest.WebVec", "limit": 50, "options": {"embed": "text"}}
	Set result = ..LoadAndWait(request)
	Do $$$AssertEquals(result.status, "ok", result.%ToJSON())
	Set rows = ..GetJson("/api/rows?table=HFTest.WebVec&n=2")
	For i = 0:1:rows.columns.%Size() - 1 {
		Do $$$AssertNotEquals(rows.columns.%Get(i), "text_embedding", "VECTOR columns are not listed")
	}
	Set out = ..GetJson("/api/similar?table=HFTest.WebVec&column=text_embedding&n=3&q=" _ $ZConvert("a boring film", "O", "URL"))
	Do $$$AssertEquals(out.rows.%Size(), 3, out.%ToJSON())
	Do $$$AssertEquals(out.columns.%Get(0), "score")
	Do $$$AssertTrue(out.sql [ "VECTOR_COSINE", "the SQL is shown")
}

Method TestBadRequests()
{
	Do $$$AssertEquals(..Call("POST", "/api/load", .body, "{""dataset"":""x/y"",""target"":""A.B""}", , "text/plain"), 415, "only JSON bodies")
	Do $$$AssertEquals(..Call("POST", "/api/load", .body, "{""dataset"":""x/y""}"), 400, "target required")
	Do $$$AssertEquals(..Call("GET", "/api/load/999999999", .body), 404, "unknown job")
	Do $$$AssertEquals(..Call("GET", "/api/rows?table=" _ $ZConvert("HFTest.Web;DROP", "O", "URL"), .body), 404, "table name checked")
	Do $$$AssertEquals(..Call("GET", "/api/rows?table=HFTest.NoSuchTable", .body), 404, "missing table")
}

}
