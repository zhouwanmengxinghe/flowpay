package dev.flowpay.support;

import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.rabbitmq.client.Channel;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

class DispatchMessagingTest {
    @Test void unsupportedNotificationIsRejectedWithoutRequeueOrDispatch() throws Exception {
        var rabbit=mock(RabbitTemplate.class);
        var support=mock(SupportService.class);
        var messaging=new DispatchMessaging(mock(JdbcTemplate.class),mock(TransactionTemplate.class),
            rabbit,new Json(new ObjectMapper()),mock(SingleInstanceLease.class),support);
        var properties=new MessageProperties(); properties.setDeliveryTag(17L);
        var message=new Message("{\"schema_version\":\"wrong\",\"run_id\":\"run_1\"}"
            .getBytes(StandardCharsets.UTF_8),properties);
        var channel=mock(Channel.class);

        messaging.consume(message,channel);

        verify(channel).basicReject(17L,false);
        verify(channel,never()).basicNack(anyLong(),anyBoolean(),anyBoolean());
        verifyNoInteractions(support);
    }
}
