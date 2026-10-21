package dev.flowpay.support;

import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import java.util.HashMap;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** 定期聚合，抓取 Prometheus 不扫描业务表；标签仅使用固定状态和执行模式。 */
@Component
public class BusinessMetrics {
    private final JdbcTemplate db;
    private volatile Map<String,Double> snapshot=Map.of();
    public BusinessMetrics(JdbcTemplate db,MeterRegistry registry,AgentGateway agent) {
        this.db=db;
        Gauge.builder("flowpay.worker.active",agent,AgentGateway::activeCount).register(registry);
        Gauge.builder("flowpay.execution.slots.active",this,self->self.snapshot.getOrDefault("global:active_slots",0.0)).register(registry);
        Gauge.builder("flowpay.outbox.pending",this,self->self.snapshot.getOrDefault("global:outbox_pending",0.0)).register(registry);
        for(String mode:new String[]{"mock","real","legacy"}) {
            for(String status:new String[]{"queued","running","completed","waiting_user","failed","canceled"}) {
                String key=mode+":"+status;
                Gauge.builder("flowpay.runs",this,self->self.snapshot.getOrDefault(key,0.0)).tag("mode",mode).tag("status",status).register(registry);
            }
            for(String measure:new String[]{"model_calls","tool_calls","input_tokens","output_tokens"}) {
                String key=mode+":"+measure;
                Gauge.builder("flowpay.execution."+measure,this,self->self.snapshot.getOrDefault(key,0.0)).tag("mode",mode).register(registry);
            }
        }
    }
    @Scheduled(fixedDelay=5000,initialDelay=5000)
    public void refresh() {
        Map<String,Double> next=new HashMap<>();
        next.put("global:active_slots",db.queryForObject("SELECT count(*) FROM execution_slots WHERE execution_id IS NOT NULL",Long.class).doubleValue());
        next.put("global:outbox_pending",db.queryForObject("SELECT count(*) FROM outbox_events WHERE published_at IS NULL",Long.class).doubleValue());
        for(var row:db.queryForList("SELECT execution_mode,status,count(*) AS n FROM runs GROUP BY execution_mode,status"))
            next.put(row.get("execution_mode")+":"+row.get("status"),((Number)row.get("n")).doubleValue());
        for(var row:db.queryForList("SELECT execution_mode,COALESCE(sum((metrics->>'model_calls')::bigint),0) AS model_calls,COALESCE(sum((metrics->>'tool_calls')::bigint),0) AS tool_calls,COALESCE(sum((metrics->>'input_tokens')::bigint),0) AS input_tokens,COALESCE(sum((metrics->>'output_tokens')::bigint),0) AS output_tokens FROM runs GROUP BY execution_mode"))
            for(String measure:new String[]{"model_calls","tool_calls","input_tokens","output_tokens"})
                next.put(row.get("execution_mode")+":"+measure,((Number)row.get(measure)).doubleValue());
        snapshot=Map.copyOf(next);
    }
}
