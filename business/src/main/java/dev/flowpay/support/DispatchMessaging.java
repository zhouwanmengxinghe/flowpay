package dev.flowpay.support;

import com.rabbitmq.client.Channel;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

@Configuration
@ConditionalOnProperty(name="support.dispatch-notifications",havingValue="true")
class DispatchMessagingConfig {
    static final String EXCHANGE="flowpay.dispatch", QUEUE="flowpay.dispatch.v1", KEY="run.queued";
    static final String DLX="flowpay.dispatch.dlx", DLQ="flowpay.dispatch.dead";
    @Bean DirectExchange dispatchExchange() { return new DirectExchange(EXCHANGE,true,false); }
    @Bean DirectExchange deadExchange() { return new DirectExchange(DLX,true,false); }
    @Bean Queue dispatchQueue() { return QueueBuilder.durable(QUEUE).deadLetterExchange(DLX).deadLetterRoutingKey("dead").build(); }
    @Bean Queue deadQueue() { return QueueBuilder.durable(DLQ).build(); }
    @Bean Binding dispatchBinding(Queue dispatchQueue,DirectExchange dispatchExchange) { return BindingBuilder.bind(dispatchQueue).to(dispatchExchange).with(KEY); }
    @Bean Binding deadBinding(Queue deadQueue,DirectExchange deadExchange) { return BindingBuilder.bind(deadQueue).to(deadExchange).with("dead"); }
}

/** outbox 可重复发布，消费者只唤醒数据库协调器，因此重复通知安全。 */
@Component
@ConditionalOnProperty(name="support.dispatch-notifications",havingValue="true")
public class DispatchMessaging {
    private final JdbcTemplate db; private final TransactionTemplate tx; private final RabbitTemplate rabbit;
    private final Json json; private final SingleInstanceLease instance; private final SupportService support;
    public DispatchMessaging(JdbcTemplate db,TransactionTemplate tx,RabbitTemplate rabbit,Json json,
                             SingleInstanceLease instance,SupportService support) {
        this.db=db; this.tx=tx; this.rabbit=rabbit; this.json=json; this.instance=instance; this.support=support;
        rabbit.setMandatory(true);
    }
    @Scheduled(fixedDelay=1000,initialDelay=2000)
    public void publish() {
        Map<String,Object> event=tx.execute(status->{
            var rows=db.queryForList("SELECT id,run_id,schema_version,payload FROM outbox_events WHERE published_at IS NULL AND available_at<=now() AND (publisher_lease_until IS NULL OR publisher_lease_until<now()) ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED");
            if(rows.isEmpty()) return null;
            var row=rows.getFirst();
            db.update("UPDATE outbox_events SET publisher_owner=?,publisher_lease_until=now()+interval '10 seconds',attempt=attempt+1 WHERE id=?",instance.instanceId(),row.get("id"));
            return row;
        });
        if(event==null) return;
        String id=(String)event.get("id");
        try {
            var correlation=new CorrelationData(id);
            rabbit.convertAndSend(DispatchMessagingConfig.EXCHANGE,DispatchMessagingConfig.KEY,
                json.write(Map.of("event_id",id,"run_id",event.get("run_id"),"schema_version",event.get("schema_version"))),
                message->{message.getMessageProperties().setDeliveryMode(MessageDeliveryMode.PERSISTENT); return message;},correlation);
            var confirm=correlation.getFuture().get(3,TimeUnit.SECONDS);
            if(!confirm.isAck()||correlation.getReturned()!=null) throw new IllegalStateException("Notification not routed");
            db.update("UPDATE outbox_events SET published_at=now(),publisher_lease_until=null WHERE id=? AND publisher_owner=?",id,instance.instanceId());
        } catch(Exception error) {
            db.update("UPDATE outbox_events SET available_at=now()+LEAST(attempt,30)*interval '1 second',publisher_owner=null,publisher_lease_until=null WHERE id=? AND publisher_owner=?",id,instance.instanceId());
        }
    }
    @RabbitListener(queues=DispatchMessagingConfig.QUEUE,ackMode="MANUAL")
    public void consume(Message message,Channel channel) throws Exception {
        long tag=message.getMessageProperties().getDeliveryTag();
        try {
            var body=json.object(new String(message.getBody(),StandardCharsets.UTF_8));
            Object version=body.get("schema_version");
            if(!(version instanceof Number number)||number.intValue()!=1||!(body.get("run_id") instanceof String))
                throw new IllegalArgumentException("Unsupported notification");
            support.dispatch();
            channel.basicAck(tag,false);
        } catch(IllegalArgumentException error) {
            channel.basicReject(tag,false);
        } catch(Exception error) {
            channel.basicNack(tag,false,true);
        }
    }
}
