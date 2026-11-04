/// iris-hf-stream: load a Hugging Face dataset into an IRIS table with one line,
/// from SQL or from ObjectScript.
///
/// <example>
/// CALL HF.LoadDataset('stanfordnlp/imdb', 'train', 'Demo.Imdb', 1000)
/// </example>
/// <example>
/// write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 1000)
/// write ##class(HF.Loader).Load("stanfordnlp/imdb", "test", "Demo.Imdb", 1000, , , {"mode":"append","embed":"text"})
/// </example>
///
/// The ObjectScript layer is only the public API. The dataset is read with the
/// Python package <code>hfstream</code> (Embedded Python), which streams the rows,
/// infers the column types and inserts them in batches.
Class HF.Loader Extends %RegisteredObject
{

/// Streams <var>dataset</var> (a Hugging Face id such as "stanfordnlp/imdb") into the table
/// <var>target</var> (schema.table).<br/>
/// <var>split</var>: split name, "train" when empty, "all" for every split (adds an hf_split column).<br/>
/// <var>limit</var>: maximum number of rows to load (per split with "all"), 0 loads everything.<br/>
/// <var>config</var>: dataset configuration name, for datasets that have several.<br/>
/// <var>batchSize</var>: rows per transaction (default 1000).<br/>
/// <var>options</var>: optional JSON object (a string, or a %DynamicObject from ObjectScript):
/// <ul>
/// <li><code>mode</code>: "fail" (default: the table must not exist), "replace" or "append"</li>
/// <li><code>columns</code>: list of the dataset columns to load</li>
/// <li><code>embed</code>: column(s) whose text gets a VECTOR embedding column</li>
/// <li><code>embed_model</code>: model2vec model id (default "minishlab/potion-base-8M")</li>
/// <li><code>sample_size</code>: rows sampled to infer the column types (default 1000)</li>
/// </ul>
/// Returns a JSON string: <code>{"status":"ok","table":...,"rows_loaded":...,"seconds":...,"columns":[...]}</code>
/// or <code>{"status":"error","code":...,"message":...}</code>. It does not throw.
ClassMethod Load(dataset As %String(MAXLEN=""), split As %String(MAXLEN=""), target As %String(MAXLEN=""), limit As %Integer = 0, config As %String(MAXLEN="") = "", batchSize As %Integer = 1000, options As %String(MAXLEN="") = "") As %String(MAXLEN="") [ SqlName = LoadDataset, SqlProc ]
{
	Set result = ""
	Try {
		Set result = ..LoadPy(..Arg(dataset), ..Arg(split), ..Arg(target), limit, ..Arg(config), batchSize, ..Options(options))
	} Catch ex {
		Set result = ..ErrorJson("INTERNAL", ex.DisplayString())
	}
	Quit result
}

/// Dry run: samples the first <var>sampleSize</var> rows of the dataset and returns, as JSON,
/// the columns and SQL types that <method>Load</method> would create, plus the split sizes
/// published on the Hub. Nothing is written. <var>options</var> as in <method>Load</method>.
ClassMethod Info(dataset As %String(MAXLEN=""), split As %String(MAXLEN="") = "train", config As %String(MAXLEN="") = "", sampleSize As %Integer = 1000, options As %String(MAXLEN="") = "") As %String(MAXLEN="") [ SqlName = DatasetInfo, SqlProc ]
{
	Set result = ""
	Try {
		Set result = ..InfoPy(..Arg(dataset), ..Arg(split), ..Arg(config), sampleSize, ..Options(options))
	} Catch ex {
		Set result = ..ErrorJson("INTERNAL", ex.DisplayString())
	}
	Quit result
}

/// Searches the Hugging Face Hub for datasets matching <var>term</var>, most downloaded first.
/// Returns JSON: <code>{"status":"ok","term":...,"results":[{"id":...,"downloads":...,"likes":...}]}</code>.
ClassMethod Search(term As %String(MAXLEN=""), limit As %Integer = 10) As %String(MAXLEN="") [ SqlName = SearchDatasets, SqlProc ]
{
	Set result = ""
	Try {
		Set result = ..SearchPy(..Arg(term), limit)
	} Catch ex {
		Set result = ..ErrorJson("INTERNAL", ex.DisplayString())
	}
	Quit result
}

/// Embeds <var>text</var> with the same model used by the <code>embed</code> option and returns
/// the vector as text, ready for TO_VECTOR. Use it for semantic search:
/// <example>
/// SELECT TOP 5 text FROM Demo.Imdb
/// ORDER BY VECTOR_COSINE(text_embedding, TO_VECTOR(HF.Embed('a moving story'), FLOAT, 256)) DESC
/// </example>
/// Unlike the other methods it raises an error on failure, so a query never silently matches nothing.
ClassMethod Embed(text As %String(MAXLEN=""), model As %String(MAXLEN="") = "") As %String(MAXLEN="") [ SqlName = Embed, SqlProc ]
{
	Quit ..EmbedPy(..Arg(text), ..Arg(model))
}

ClassMethod LoadPy(dataset As %String, split As %String, target As %String, limit As %Integer, config As %String, batchSize As %Integer, options As %String) As %String [ Language = python, Private ]
{
    from hfstream import load_to_iris
    return load_to_iris(dataset, split, target, limit, config, batchSize, options)
}

ClassMethod InfoPy(dataset As %String, split As %String, config As %String, sampleSize As %Integer, options As %String) As %String [ Language = python, Private ]
{
    from hfstream import dataset_info
    return dataset_info(dataset, split, config, sampleSize, options)
}

ClassMethod SearchPy(term As %String, limit As %Integer) As %String [ Language = python, Private ]
{
    from hfstream import search_datasets
    return search_datasets(term, limit)
}

ClassMethod EmbedPy(text As %String, model As %String) As %String [ Language = python, Private ]
{
    from hfstream import embed_text
    return embed_text(text, model)
}

/// From SQL, the empty string literal '' arrives as $CHAR(0); treat it as "not given".
ClassMethod Arg(value As %String) As %String [ Private ]
{
	Quit $Select(value = $Char(0): "", 1: value)
}

/// Options may be given as a JSON string or, from ObjectScript, as a dynamic object.
ClassMethod Options(options) As %String [ Private ]
{
	If $IsObject(options) Quit options.%ToJSON()
	Quit ..Arg(options)
}

ClassMethod ErrorJson(code As %String, message As %String) As %String [ Private ]
{
	Set obj = {"status":"error"}
	Set obj.code = code
	Set obj.message = message
	Quit obj.%ToJSON()
}

}
