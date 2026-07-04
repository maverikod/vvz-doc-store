def test_import_packages():
    import doc_store_client
    import doc_store_filewatcher
    import doc_store_server

    assert doc_store_client is not None
    assert doc_store_filewatcher is not None
    assert doc_store_server is not None
