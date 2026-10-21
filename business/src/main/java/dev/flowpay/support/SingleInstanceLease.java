package dev.flowpay.support;

import jakarta.annotation.PreDestroy;
import java.sql.Connection;
import java.util.UUID;
import javax.sql.DataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.sql.init.dependency.DependsOnDatabaseInitialization;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/** 单实例使用会话级 advisory lock；分布式实例使用数据库心跳和任务级租约。 */
@Component
@DependsOnDatabaseInitialization
public class SingleInstanceLease {
    private final Connection connection;
    private final JdbcTemplate db;
    private final String mode,instanceId;
    private volatile boolean healthy=true;
    private int missedHeartbeats=0;
    public SingleInstanceLease(DataSource source,JdbcTemplate db,TransactionTemplate tx,
        @Value("${support.scheduler-mode:single}") String mode,
        @Value("${support.instance-name:local}") String name,
        @Value("${support.global-slots:2}") int globalSlots,
        @Value("${support.run-lease-seconds:30}") int leaseSeconds,
        @Value("${support.heartbeat-seconds:5}") int heartbeatSeconds,
        @Value("${support.run-deadline-seconds:180}") int deadlineSeconds) throws Exception {
        if(!mode.equals("single")&&!mode.equals("distributed")) throw new IllegalArgumentException("Invalid scheduler mode");
        if(globalSlots!=2) throw new IllegalArgumentException("This schema supports exactly two global slots");
        if(heartbeatSeconds<1||heartbeatSeconds*3>=leaseSeconds||deadlineSeconds<leaseSeconds)
            throw new IllegalArgumentException("Invalid heartbeat, lease, or deadline combination");
        this.db=db; this.mode=mode; this.instanceId=name+"-"+UUID.randomUUID();
        Connection owned=null;
        if(mode.equals("single")) {
            owned=source.getConnection();
            try(var statement=owned.createStatement(); var rows=statement.executeQuery("SELECT pg_try_advisory_lock(724091,1)")) {
                rows.next();
                if(!rows.getBoolean(1)) { owned.close(); throw new IllegalStateException("This database already has a single Java scheduler"); }
            } catch(Exception e) { if(!owned.isClosed()) owned.close(); throw e; }
        }
        try {
            tx.executeWithoutResult(status->{
                db.queryForList("SELECT pg_advisory_xact_lock(724091,3)");
                db.update("DELETE FROM scheduler_instances WHERE last_seen<now()-interval '20 seconds'");
                var incompatible=db.queryForList("SELECT instance_id FROM scheduler_instances WHERE scheduler_mode<>? AND last_seen>=now()-interval '20 seconds'",mode);
                if(!incompatible.isEmpty()) throw new IllegalStateException("Active scheduler uses another mode");
                db.update("INSERT INTO scheduler_instances(instance_id,scheduler_mode) VALUES (?,?)",instanceId,mode);
            });
        } catch(Exception error) {
            if(owned!=null&&!owned.isClosed()) owned.close();
            throw error;
        }
        connection=owned;
    }
    public String instanceId() { return instanceId; }
    public String mode() { return mode; }
    public synchronized void check() {
        if(!healthy) throw new ApiError(503,"运行租约失效，请重启服务后恢复");
        try {
            if(connection!=null&&(connection.isClosed()||!connection.isValid(2))) {
                healthy=false;
                throw new ApiError(503,"运行租约失效，请重启服务后恢复");
            }
            Integer active=db.queryForObject("SELECT count(*) FROM scheduler_instances WHERE instance_id=? AND last_seen>=now()-interval '20 seconds'",Integer.class,instanceId);
            if(active==null||active!=1) {
                healthy=false;
                throw new ApiError(503,"运行租约失效，请重启服务后恢复");
            }
        } catch(ApiError error) { throw error; }
        catch(Exception e) {
            // 一次数据库抖动只让当前请求失败；任务租约和连续心跳失败负责真正隔离实例。
            throw new ApiError(503,"运行租约暂时无法确认，请稍后重试");
        }
    }
    @Scheduled(fixedDelay=5000,initialDelay=3000)
    public synchronized void heartbeat() {
        try {
            int changed=db.update("UPDATE scheduler_instances SET last_seen=now() WHERE instance_id=?",instanceId);
            if(changed!=1) healthy=false;
            else missedHeartbeats=0;
        } catch(Exception e) {
            missedHeartbeats++;
            if(missedHeartbeats>=3) healthy=false;
        }
    }
    @PreDestroy public void close() throws Exception {
        try { db.update("DELETE FROM scheduler_instances WHERE instance_id=?",instanceId); } catch(Exception ignored) {}
        if(connection!=null) connection.close();
    }
}
