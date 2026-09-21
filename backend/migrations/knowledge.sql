-- 知识库：保存集合、文档与分块；通过约束保护向量维度和就绪集合的不可变性。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE collections (
 collection_id text PRIMARY KEY, dataset_release_id text NOT NULL,
 dataset_manifest_sha256 text NOT NULL CHECK(dataset_manifest_sha256 ~ '^[0-9a-f]{64}$'),
 build_config jsonb NOT NULL CHECK(jsonb_typeof(build_config)='object'),
 dimensions integer NOT NULL CHECK(dimensions>0),
 status text NOT NULL DEFAULT 'building' CHECK(status IN ('building','ready')),
 created_at timestamptz NOT NULL DEFAULT now(), ready_at timestamptz,
 CHECK((status='ready')=(ready_at IS NOT NULL))
);
CREATE TABLE documents (
 collection_id text NOT NULL REFERENCES collections, doc_id text NOT NULL,
 title text NOT NULL, source_path text NOT NULL, content text NOT NULL,
 source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 metadata jsonb NOT NULL CHECK(jsonb_typeof(metadata)='object'),
 PRIMARY KEY(collection_id,doc_id)
);
-- embedding 不在表类型上固定维度，而按所属集合校验，允许不同集合使用不同模型。
-- 查询必须先限定集合，不能直接跨集合做向量距离比较。
CREATE TABLE chunks (
 collection_id text NOT NULL, doc_id text NOT NULL, chunk_no integer NOT NULL CHECK(chunk_no>0),
 content text NOT NULL CHECK(length(trim(content))>0),
 source_locator jsonb NOT NULL CHECK(jsonb_typeof(source_locator)='object'),
 embedding vector,
 PRIMARY KEY(collection_id,doc_id,chunk_no),
 FOREIGN KEY(collection_id,doc_id) REFERENCES documents
);
-- 集合发布后固定其内容和构建参数；新模型或新切块策略需创建新集合。
-- 发布时还要检查无缺块、编号连续且向量齐全，避免只依赖调用方自报成功。
CREATE FUNCTION guard_collection() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Knowledge snapshots cannot be deleted'; END IF;
 IF OLD.status='ready' THEN RAISE EXCEPTION 'Ready collection is immutable'; END IF;
 IF (to_jsonb(NEW)-'status'-'ready_at') IS DISTINCT FROM (to_jsonb(OLD)-'status'-'ready_at') THEN
   RAISE EXCEPTION 'Build configuration is immutable';
 END IF;
 IF NEW.status='ready' THEN
   IF NOT EXISTS(SELECT 1 FROM documents WHERE collection_id=NEW.collection_id)
     OR EXISTS(SELECT 1 FROM documents d WHERE d.collection_id=NEW.collection_id AND NOT EXISTS
         (SELECT 1 FROM chunks c WHERE c.collection_id=d.collection_id AND c.doc_id=d.doc_id))
     OR EXISTS(SELECT 1 FROM chunks WHERE collection_id=NEW.collection_id AND embedding IS NULL)
     OR EXISTS(SELECT 1 FROM chunks WHERE collection_id=NEW.collection_id GROUP BY doc_id
               HAVING min(chunk_no)<>1 OR max(chunk_no)<>count(*)) THEN
     RAISE EXCEPTION 'Collection incomplete';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER collection_guard BEFORE UPDATE OR DELETE ON collections
 FOR EACH ROW EXECUTE FUNCTION guard_collection();
-- 文档不可更新；分块唯一允许的更新是给 NULL embedding 补值。
-- 对集合行加锁，使补向量和标记 ready 串行，防止发布检查与写入互相穿插。
CREATE FUNCTION guard_knowledge_row() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE collection collections%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Knowledge rows cannot be deleted'; END IF;
 SELECT * INTO STRICT collection FROM collections WHERE collection_id=NEW.collection_id FOR UPDATE;
 IF collection.status<>'building' THEN RAISE EXCEPTION 'Ready collection is immutable'; END IF;
 IF TG_TABLE_NAME='documents' AND TG_OP='UPDATE' THEN
   RAISE EXCEPTION 'Document is immutable';
 END IF;
 IF TG_TABLE_NAME='chunks' THEN
   IF TG_OP='UPDATE' AND ((to_jsonb(NEW)-'embedding') IS DISTINCT FROM (to_jsonb(OLD)-'embedding')
                         OR OLD.embedding IS NOT NULL) THEN
     RAISE EXCEPTION 'Only missing embeddings may be filled';
   END IF;
   IF NEW.embedding IS NOT NULL AND
      (vector_dims(NEW.embedding)<>collection.dimensions OR vector_norm(NEW.embedding)=0) THEN
     RAISE EXCEPTION 'Invalid vector dimensions or norm';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER documents_guard BEFORE INSERT OR UPDATE OR DELETE ON documents
 FOR EACH ROW EXECUTE FUNCTION guard_knowledge_row();
CREATE TRIGGER chunks_guard BEFORE INSERT OR UPDATE OR DELETE ON chunks
 FOR EACH ROW EXECUTE FUNCTION guard_knowledge_row();
