"""Gql queries"""

HISTORIC_DATA = """
                {{
                  viewer {{
                    home(id: "{0}") {{
                      {1}(resolution: {2}, last: {3}, before: "{5}") {{
                        pageInfo {{
                          hasPreviousPage
                          startCursor
                        }}
                        nodes {{
                          from
                          unitPrice
                          {4}
                          {1}
                        }}
                      }}
                    }}
                  }}
                }}
          """
HISTORIC_DATA_DATE = """
                    {{
                      viewer {{
                        home(id: "{0}") {{
                          {1}(resolution: {2}, first: {3}, after: "{4}") {{
                            nodes {{
                              from
                              to
                              unitPrice
                              unitPriceVAT
                              currency
                              {5}
                            }}
                          }}
                        }}
                      }}
                    }}
                    """
INFO = """
        {
          viewer {
            name
            userId
            homes {
              id
              subscriptions {
                status
              }
            }
          }
        }
        """
PUSH_NOTIFICATION = """
        mutation{{
          sendPushNotification(input: {{
            title: "{}",
            message: "{}",
          }}){{
            successful
            pushedToNumberOfDevices
          }}
        }}
        """
UPDATE_INFO = """
        {
          viewer {
            home(id: "%s") {
              appNickname
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
            }
          }
        }
        """
