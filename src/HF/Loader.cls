/// iris-hf-stream: load a Hugging Face dataset into an IRIS table with one line,
/// from SQL or from ObjectScript.
///
/// <example>
/// CALL HF.LoadDataset('stanfordnlp/imdb', 'train', 'Demo.Imdb', 1000)
/// </example>
/// <example>
/// write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 1000)
/// </example>
///
/// The ObjectScript layer is only the public API. The dataset is read with the
/// Python package <code>hfstream</code> (Embedded Python), which streams the rows,
/// infers the column types and inserts them in batches.
Class HF.Loader Extends %RegisteredObject
{

/// Streams <var>dataset</var> (a Hugging Face id such as "stanfordnlp/imdb") into the new table
/// <var>target</var> (schema.table).<br/>
/// <var>split</var>: split name, "train" when empty.<br/>
/// <var>limit</var>: maximum number of rows to load, 0 loads the whole split.<br/>
/// <var>config</var>: dataset configuration name, for datasets that have several.<br/>
/// <var>batchSize</var>: rows per transaction (default 1000).<br/>
/// Returns a JSON string: <code>{"status":"ok","table":...,"rows_loaded":...,"seconds":...,"columns":[...]}</code>
/// or <code>{"status":"error","code":...,"message":...}</code>. It does not throw.
ClassMethod Load(dataset As %String, split As %String, target As %String, limit As %Integer = 0, config As %String = "", batchSize As %Integer = 1000) As %String [ SqlName = LoadDataset, SqlProc ]
{
	Set result = ""
	Try {
		Set result = ..LoadPy(..Arg(dataset), ..Arg(split), ..Arg(target), limit, ..Arg(config), batchSize)
	} Catch ex {
		Set result = ..ErrorJson("INTERNAL", ex.DisplayString())
	}
	Quit result
}

/// Dry run: samples the first <var>sampleSize</var> rows of the dataset and returns, as JSON,
/// the columns and SQL types that <method>Load</method> would create. Nothing is written.
ClassMethod Info(dataset As %String, split As %String = "train", config As %String = "", sampleSize As %Integer = 1000) As %String [ SqlName = DatasetInfo, SqlProc ]
{
	Set result = ""
	Try {
		Set result = ..InfoPy(..Arg(dataset), ..Arg(split), ..Arg(config), sampleSize)
	} Catch ex {
		Set result = ..ErrorJson("INTERNAL", ex.DisplayString())
	}
	Quit result
}

ClassMethod LoadPy(dataset As %String, split As %String, target As %String, limit As %Integer, config As %String, batchSize As %Integer) As %String [ Language = python, Private ]
{
    from hfstream import load_to_iris
    return load_to_iris(dataset, split, target, limit, config, batchSize)
}

ClassMethod InfoPy(dataset As %String, split As %String, config As %String, sampleSize As %Integer) As %String [ Language = python, Private ]
{
    from hfstream import dataset_info
    return dataset_info(dataset, split, config, sampleSize)
}

/// From SQL, the empty string literal '' arrives as $CHAR(0); treat it as "not given".
ClassMethod Arg(value As %String) As %String [ Private ]
{
	Quit $Select(value = $Char(0): "", 1: value)
}

ClassMethod ErrorJson(code As %String, message As %String) As %String [ Private ]
{
	Set obj = {"status":"error"}
	Set obj.code = code
	Set obj.message = message
	Quit obj.%ToJSON()
}

}
