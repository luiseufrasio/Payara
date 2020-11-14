/*
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS HEADER.
 *
 * Copyright (c) [2020] Payara Foundation and/or its affiliates. All rights reserved.
 *
 * The contents of this file are subject to the terms of either the GNU
 * General Public License Version 2 only ("GPL") or the Common Development
 * and Distribution License("CDDL") (collectively, the "License").  You
 * may not use this file except in compliance with the License.  You can
 * obtain a copy of the License at
 * https://github.com/payara/Payara/blob/master/LICENSE.txt
 * See the License for the specific
 * language governing permissions and limitations under the License.
 *
 * When distributing the software, include this License Header Notice in each
 * file and include the License file at glassfish/legal/LICENSE.txt.
 *
 * GPL Classpath Exception:
 * The Payara Foundation designates this particular file as subject to the "Classpath"
 * exception as provided by the Payara Foundation in the GPL Version 2 section of the License
 * file that accompanied this code.
 *
 * Modifications:
 * If applicable, add the following below the License Header, with the fields
 * enclosed by brackets [] replaced by your own identifying information:
 * "Portions Copyright [year] [name of copyright owner]"
 *
 * Contributor(s):
 * If you wish your version of this file to be governed by only the CDDL or
 * only the GPL Version 2, indicate your decision by adding "[Contributor]
 * elects to include this software in this distribution under the [CDDL or GPL
 * Version 2] license."  If you don't indicate a single choice of license, a
 * recipient has the option to distribute your version of this file under
 * either the CDDL, the GPL Version 2 or to extend the choice of license to
 * its licensees as provided above.  However, if you add GPL Version 2 code
 * and therefore, elected the GPL Version 2 license, then the option applies
 * only if the new code is made subject to such option by the copyright
 * holder.
 */
package fish.payara.microprofile.config.extensions.hashicrop;

import java.io.IOException;
import java.io.InputStream;
import java.io.Reader;
import java.io.StringReader;
import java.security.KeyStoreException;
import java.security.NoSuchAlgorithmException;
import java.security.UnrecoverableKeyException;
import java.security.cert.CertificateException;
import java.util.HashMap;
import java.util.Map;
import java.util.logging.Level;
import java.util.logging.Logger;

import javax.json.Json;
import javax.json.JsonException;
import javax.json.stream.JsonParser;
import javax.json.stream.JsonParser.Event;
import javax.ws.rs.ProcessingException;
import javax.ws.rs.client.Client;
import javax.ws.rs.client.ClientBuilder;
import javax.ws.rs.client.Entity;
import javax.ws.rs.client.WebTarget;
import javax.ws.rs.core.MediaType;
import javax.ws.rs.core.Response;

import org.glassfish.config.support.TranslatedConfigView;
import org.jvnet.hk2.annotations.Service;

import com.fasterxml.jackson.core.JsonParseException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonMappingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import fish.payara.microprofile.config.extensions.hashicrop.model.SecretHolder;
import fish.payara.nucleus.microprofile.config.source.extension.ConfiguredExtensionConfigSource;

@Service(name = "hashicrop-secrets-config-source")
public class HashiCropSecretsConfigSource extends ConfiguredExtensionConfigSource<HashiCropSecretsConfigSourceConfiguration> {

    private static final Logger LOGGER = Logger.getLogger(HashiCropSecretsConfigSource.class.getName());

    private Client client = ClientBuilder.newClient();
    private String hashiCropVaultToken;
    private final ObjectMapper mapper = new ObjectMapper();

    @Override
    public void bootstrap() {
        try {
            // Get the HashiCrop Vault token.
            hashiCropVaultToken = TranslatedConfigView.getRealPasswordFromAlias("${ALIAS=HASHICROP_VAULT_TOKEN}");
        } catch (KeyStoreException | CertificateException | IOException | NoSuchAlgorithmException | UnrecoverableKeyException ex) {
            LOGGER.log(Level.WARNING, "Unable to get value from password aliases", ex);

        }
    }

    @Override
    public Map<String, String> getProperties() {
        Map<String, String> results = new HashMap<>();

        if (hashiCropVaultToken == null) {
            printMisconfigurationMessage();
            return results;
        }

        final WebTarget secretsTarget = client
                .target(configuration.getVaultAddress());

        final Response secretsResponse = secretsTarget
                .request()
                .accept(MediaType.APPLICATION_JSON)
                .header("Authorization", "Bearer " + hashiCropVaultToken)
                .get();

        if (secretsResponse.getStatus() != 200) {
            LOGGER.log(Level.WARNING, "Unable to get secrets from the vault", ex);
            return results;
        }

        try {
            final String secretString = readSecretString((InputStream) secretsResponse.getEntity());

            try (final StringReader reader = new StringReader(secretString)) {
                return readMap(reader);
            }
        } catch (ProcessingException | JsonException | IOException ex) {
            LOGGER.log(Level.WARNING, "Unable to read secret value", ex);
        }

        return results;
    }

    @Override
    public String getValue(String propertyName) {
        if (hashiCropVaultToken == null) {
            printMisconfigurationMessage();
            return null;
        }
        return getProperties().get(propertyName);
    }

    @Override
    public boolean setValue(String secretName, String secretValue) {
        if (hashiCropVaultToken == null) {
            printMisconfigurationMessage();
            return false;
        }

        Map<String, String> properties = getProperties();
        properties.put(secretName, secretValue);
        return modifySecret(properties);
    }

    private boolean modifySecret(Map<String, String> properties) {
        final WebTarget target = client
                .target(configuration.getVaultAddress());

        final Response setSecretResponse = target
                .request()
                .accept(MediaType.APPLICATION_JSON)
                .header("Content-Type", MediaType.APPLICATION_JSON)
                .header("Authorization", "Bearer " + hashiCropVaultToken)
                .put(Entity.entity(new SecretHolder(properties), MediaType.APPLICATION_JSON));

        if (setSecretResponse.getStatus() == 200) {
            return true;
        }
        LOGGER.log(Level.WARNING, "Failed to modify HashiCrop secret. {0}", setSecretResponse.readEntity(String.class));
        return false;
    }

    @Override
    public boolean deleteValue(String secretName) {
        if (hashiCropVaultToken == null) {
            printMisconfigurationMessage();
            return false;
        }

        Map<String, String> properties = getProperties();
        properties.remove(secretName);
        return modifySecret(properties);
    }

    private static String readSecretString(InputStream input) {
        try (JsonParser parser = Json.createParser(input)) {
            while (parser.hasNext()) {
                JsonParser.Event parseEvent = parser.next();
                if (parseEvent == Event.KEY_NAME) {
                    final String keyName = parser.getString();

                    parser.next();
                    if ("data".equals(keyName)) {
                        return parser.getObject().getJsonObject(keyName).toString();
                    }
                }
            }
        }
        return null;
    }

    private Map<String, String> readMap(Reader input) throws JsonParseException, JsonMappingException, IOException {
        return mapper.readValue(input, new TypeReference<Map<String, String>>() {
        });
    }

    @Override
    public String getName() {
        return "hashicrop";
    }

    private static void printMisconfigurationMessage() {
        LOGGER.warning("HashiCrop Secrets Config Source isn't configured correctly. "
                + "Make sure that the password aliases HASHICROP_VAULT_TOKEN exist.");
    }

}
