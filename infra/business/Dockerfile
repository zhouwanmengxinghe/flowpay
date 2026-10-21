FROM eclipse-temurin:21-jre
WORKDIR /app
COPY business/target/support-business-0.1.0.jar /app/support.jar
EXPOSE 8080
USER 10001
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=70", "-jar", "/app/support.jar"]
