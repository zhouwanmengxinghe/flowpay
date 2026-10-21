package dev.flowpay.support;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class SupportService implements ApplicationRunner {
    private final JdbcTemplate db; private final Identity identity; private final Json json;
    private final TransactionTemplate tx; private final AgentGateway agent;
    private final TrafficPolicy traffic; private final RunCoordinator coordinator; private final OutboxService outbox;
    private final ScheduledExecutorService heartbeats=Executors.newScheduledThreadPool(1);
    private final int heartbeatSeconds;
    public SupportService(JdbcTemplate db, Identity identity, Json json, TransactionTemplate tx, AgentGateway agent,
                          TrafficPolicy traffic,RunCoordinator coordinator,OutboxService outbox,
                          @org.springframework.beans.factory.annotation.Value("${support.heartbeat-seconds:5}") int heartbeatSeconds) {
        if(heartbeatSeconds<1) throw new IllegalArgumentException("Invalid heartbeat interval");
        this.db=db; this.identity=identity; this.json=json; this.tx=tx; this.agent=agent; this.traffic=traffic;
        this.coordinator=coordinator; this.outbox=outbox; this.heartbeatSeconds=heartbeatSeconds;
    }
    static String id(String prefix) { return prefix+"_"+UUID.randomUUID().toString().replace("-",""); }
    private Map<String,Object> one(String sql, Object... values) {
        var rows=db.queryForList(sql,values);
        if(rows.isEmpty()) throw new ApiError(404,"记录不存在或无权访问");
        return rows.getFirst();
    }
    private Map<String,Object> session(Identity.User user,String sid,boolean lock) {
        if(lock) {
            var scope=one("SELECT tenant_id FROM sessions WHERE id=?",sid);
            identity.authorize(user,(String)scope.get("tenant_id"));
            lockTenantForSession(sid);
        }
        var row=one("SELECT * FROM sessions WHERE id=?"+(lock?" FOR UPDATE":""),sid);
        identity.authorize(user,(String)row.get("tenant_id")); return row;
    }
    private void lockTenantForSession(String sid) {
        var row=one("SELECT tenant_id FROM sessions WHERE id=?",sid);
        // 所有会话状态事务遵循商户→会话→执行记录的锁顺序，含领取、完成、失败和取消。
        db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",row.get("tenant_id"));
    }
    private Map<String,Object> publicSession(Map<String,Object> row) {
        var result=new java.util.LinkedHashMap<>(row); result.remove("checkpoint"); return result;
    }
    public List<Map<String,Object>> accounts(Identity.User user) {
        return db.queryForList("SELECT a.* FROM tenant_accounts a WHERE (a.tenant_id=? AND ?='MERCHANT') OR EXISTS "
            +"(SELECT 1 FROM support_grants g WHERE g.user_id=? AND g.tenant_id=a.tenant_id)",user.tenantId(),user.role(),user.id());
    }
    public List<Map<String,Object>> sessions(Identity.User user) {
        return db.queryForList("SELECT id,tenant_id,account_id,environment,title,status,created_at FROM sessions s WHERE "
            +"(s.tenant_id=? AND ?='MERCHANT') OR EXISTS (SELECT 1 FROM support_grants g WHERE g.user_id=? AND g.tenant_id=s.tenant_id) "
            +"ORDER BY created_at DESC LIMIT 100",user.tenantId(),user.role(),user.id());
    }
    public Map<String,Object> createSession(Identity.User user,String account,String environment) {
        if(!user.role().equals("MERCHANT")) throw new ApiError(403,"仅商户可创建调查");
        if(!List.of("production","sandbox").contains(environment)) throw new ApiError(400,"环境无效");
        Integer count=db.queryForObject("SELECT count(*) FROM tenant_accounts WHERE tenant_id=? AND account_id=?",Integer.class,user.tenantId(),account);
        if(count==null||count==0) throw new ApiError(403,"账户不属于当前商户");
        String sid=id("session");
        db.update("INSERT INTO sessions(id,tenant_id,owner_id,account_id,environment) VALUES (?,?,?,?,?)",sid,user.tenantId(),user.id(),account,environment);
        return publicSession(session(user,sid,false));
    }
    public Map<String,Object> detail(Identity.User user,String sid) {
        var result=publicSession(session(user,sid,false));
        result.put("runs",db.queryForList("SELECT id,request_id,input,status,answer,error,created_at,ended_at FROM runs WHERE session_id=? ORDER BY created_at,id",sid));
        return result;
    }
    public List<Map<String,Object>> events(Identity.User user,String sid,long after) {
        session(user,sid,false);
        return db.query("SELECT seq,run_id,type,payload,created_at FROM session_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 1000",
            (r,n)->Map.<String,Object>of("seq",r.getLong("seq"),"run_id",r.getString("run_id"),"type",r.getString("type"),
                "payload",json.object(r.getString("payload")),"timestamp",r.getTimestamp("created_at").toInstant().toString()),sid,after);
    }
    private void event(String sid,String run,String type,Object payload) {
        Long seq=db.queryForObject("UPDATE sessions SET next_seq=next_seq+1 WHERE id=? RETURNING next_seq",Long.class,sid);
        db.update("INSERT INTO session_events(session_id,seq,run_id,type,payload) VALUES (?,?,?,?,?::jsonb)",sid,seq,run,type,json.write(payload));
    }
    public Map<String,Object> submit(Identity.User user,String sid,String requestId,String content) {
        if(!user.role().equals("MERCHANT")) throw new ApiError(403,"仅商户可提交调查");
        String text=content.strip(); if(text.isEmpty()) throw new ApiError(400,"问题不能为空");
        // 会话行锁串行化检查和受理；模型调用必须在事务外，避免长时间占用连接和锁。
        var reservation=tx.execute(status->{
            // 统一受理锁只覆盖短事务，使全局队列上限与商户配额在并发下准确。
            db.queryForList("SELECT pg_advisory_xact_lock(724091,2)");
            db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",user.tenantId());
            var current=session(user,sid,true);
            var prior=db.queryForList("SELECT id,input,status FROM runs WHERE session_id=? AND request_id=?",sid,requestId);
            if(!prior.isEmpty()) {
                if(!prior.getFirst().get("input").equals(text)) throw new ApiError(409,"幂等键已用于不同内容");
                return Map.<String,Object>of("id",prior.getFirst().get("id"),"status",prior.getFirst().get("status"),"existing",true);
            }
            if(List.of("queued","running").contains(current.get("status"))) throw new ApiError(409,"当前会话已有执行任务");
            traffic.admit(user.tenantId());
            String run=id("run");
            db.update("INSERT INTO runs(id,session_id,request_id,input,status) VALUES (?,?,?,?,'queued')",run,sid,requestId,text);
            db.update("UPDATE sessions SET status='queued',title=? WHERE id=?",text.substring(0,Math.min(100,text.length())),sid);
            event(sid,run,"run.queued",Map.of("content",text));
            outbox.queued(run);
            return Map.<String,Object>of("id",run,"status","queued","existing",false);
        });
        // 数据库是待执行任务的权威来源；即便提交后进程退出，调度器重启仍可领取。
        return reservation;
    }
    private void execute(RunCoordinator.Claim claim) {
        ScheduledFuture<?> heartbeat=null;
        try {
            heartbeat=heartbeats.scheduleAtFixedRate(()->{
                if(!coordinator.heartbeat(claim)) agent.cancel(claim.executionId());
            },heartbeatSeconds,heartbeatSeconds,TimeUnit.SECONDS);
            var input=tx.execute(status->{
                var current=session(claim.owner(),claim.sessionId(),false);
                List<Map<String,Object>> history=new ArrayList<>();
                for(var previous:db.queryForList("SELECT input,answer FROM (SELECT input,answer,created_at,id FROM runs WHERE session_id=? AND status IN ('completed','waiting_user') ORDER BY created_at DESC,id DESC LIMIT 40) h ORDER BY created_at,id",claim.sessionId())) {
                    history.add(Map.of("role","user","content",previous.get("input")));
                    history.add(Map.of("role","assistant","content",previous.get("answer")));
                }
                return Map.<String,Object>ofEntries(
                    Map.entry("tenant_id",current.get("tenant_id")),Map.entry("session_id",claim.sessionId()),
                    Map.entry("run_id",claim.runId()),Map.entry("execution_id",claim.executionId()),
                    Map.entry("claim_version",claim.version()),Map.entry("account_ids",List.of(current.get("account_id"))),
                    Map.entry("environment",current.get("environment")),Map.entry("content",claim.input()),
                    Map.entry("history",history),Map.entry("saved_state",json.object(current.get("checkpoint").toString())));
            });
            var result=agent.execute(input);
            String answer=(String)result.get("answer"), end=(String)result.get("status");
            if(answer==null||answer.isBlank()||!List.of("completed","waiting_user").contains(end))
                throw new IllegalStateException("Invalid Agent result");
            Object checkpoint=result.getOrDefault("checkpoint",Map.of());
            if(!(checkpoint instanceof Map)) throw new IllegalStateException("Invalid Agent checkpoint");
            if(json.write(checkpoint).getBytes(java.nio.charset.StandardCharsets.UTF_8).length>1_000_000)
                throw new IllegalStateException("Checkpoint budget exceeded");
            Object evidence=result.getOrDefault("evidence",List.of());
            var state=(Map<?,?>)checkpoint;
            coordinator.complete(claim,end,answer,evidence,result.getOrDefault("metrics",Map.of()),checkpoint,
                String.valueOf(state.get("_execution_mode")),String.valueOf(state.get("_execution_engine")));
        } catch(Exception e) {
            String reason=e instanceof AgentGateway.Failure failure ? switch(failure.status) {
                case 409 -> "会话与当前执行模式不兼容，请新建调查";
                case 429 -> "模型服务限流，请稍后在原会话发送新问题";
                case 503 -> "Agent 暂不可用，请稍后在原会话发送新问题";
                case 504 -> "调查超时，可在原会话发送更具体的问题";
                default -> "Agent 服务执行失败，可在原会话发送新问题";
            } : "Agent 执行失败（"+e.getClass().getSimpleName()+"），可在原会话发送新问题";
            coordinator.fail(claim,reason);
        } finally { if(heartbeat!=null) heartbeat.cancel(false); }
    }
    public void cancel(Identity.User user,String sid) {
        if(!user.role().equals("MERCHANT")) throw new ApiError(403,"仅商户可停止调查");
        tx.executeWithoutResult(status->{
            session(user,sid,true);
            coordinator.cancelSession(sid);
        });
    }
    @org.springframework.scheduling.annotation.Scheduled(fixedDelayString="${support.scan-delay-ms:500}")
    public void dispatch() {
        if(!ready) return;
        try {
            identity.checkLease();
            var deployment=agent.status();
            for(int i=agent.activeCount();i<agent.capacity();i++) {
                var claim=coordinator.claimNext(deployment);
                if(claim==null) break;
                if(!agent.dispatch(claim.executionId(),()->execute(claim))) coordinator.releaseRejected(claim);
            }
            for(var row:coordinator.pendingCancels()) {
                String execution=(String)row.get("execution_id");
                if(agent.cancel(execution)) coordinator.acknowledgeCancel(execution);
                else coordinator.cancelFailed(execution);
            }
            coordinator.recoverExpired();
        } catch(Exception e) {
            org.slf4j.LoggerFactory.getLogger(getClass()).warn("Dispatch cycle failed: {}",e.getClass().getSimpleName());
        }
    }
    private volatile boolean ready=false;
    @Override public void run(ApplicationArguments args) {
        coordinator.recoverExpired();
        ready=true;
    }
    @jakarta.annotation.PreDestroy public void close() { heartbeats.shutdownNow(); }
}
